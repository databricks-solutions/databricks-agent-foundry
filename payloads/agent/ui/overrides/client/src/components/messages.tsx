import { PreviewMessage, AwaitingResponseMessage } from './message';
import { Fragment, memo, useEffect, useRef } from 'react';
import equal from 'fast-deep-equal';
import type { UseChatHelpers } from '@ai-sdk/react';
import { useMessages } from '@/hooks/use-messages';
import type { ChatMessage, FeedbackMap } from '@chat-template/core';
import { useDataStream } from './data-stream-provider';
import { Conversation, ConversationContent } from './elements/conversation';
import { ArrowDownIcon } from 'lucide-react';
import { useCompactions } from '@/hooks/use-compactions';
import { CompactionDivider } from './compaction-divider';
import { useAppConfig } from '@/contexts/AppConfigContext';
import { cn } from '@/lib/utils';

// Agent Bricks auto-scroll override.
//
// Upstream only scrolls once, on `status === 'submitted'`, and then never
// again — so a reply longer than the `min-h-96` headroom on the last message
// runs off the bottom and the user has to scroll by hand for the rest of the
// turn. The `<Conversation>` (use-stick-to-bottom) wrapper looks like it
// should cover this, but it is inert here: `StickToBottom.Content` puts its
// scroll element on an inner `height: 100%` div whose ancestors have no
// definite height, so that div sizes to its content, `scrollHeight` always
// equals `clientHeight`, and every scrollTop the library writes is a no-op.
// The element that actually scrolls is the outer container below.
//
// Rather than restructure the layout (which would move the real scroller away
// from `useScrollToBottom`'s containerRef and break `isAtBottom` plus both
// scroll-to-bottom buttons), we drive the outer container directly.
//
// Contract: follow the bottom while content grows; a deliberate upward gesture
// hands control back to the user for the rest of the turn; returning to the
// bottom resumes following.
//
// Known gap: dragging the scrollbar fires neither wheel nor touchmove, so it
// does not release the follow. Wheel, touch and keyboard cover real usage.

/** Re-arm once the user is back within this many px of the bottom. Deliberately
 *  tighter than one wheel tick (~100px) so a single tick escapes the follow. */
const REARM_THRESHOLD_PX = 32;

/** Keys that scroll the viewport upward, i.e. away from the streaming text. */
const RELEASE_KEYS = new Set(['ArrowUp', 'PageUp', 'Home']);

interface MessagesProps {
  chatId: string;
  status: UseChatHelpers<ChatMessage>['status'];
  messages: ChatMessage[];
  setMessages: UseChatHelpers<ChatMessage>['setMessages'];
  addToolApprovalResponse: UseChatHelpers<ChatMessage>['addToolApprovalResponse'];
  sendMessage: UseChatHelpers<ChatMessage>['sendMessage'];
  regenerate: UseChatHelpers<ChatMessage>['regenerate'];
  isReadonly: boolean;
  selectedModelId: string;
  feedback?: FeedbackMap;
}

function PureMessages({
  chatId,
  status,
  messages,
  setMessages,
  addToolApprovalResponse,
  sendMessage,
  regenerate,
  isReadonly,
  selectedModelId,
  feedback = {},
}: MessagesProps) {
  const {
    containerRef: messagesContainerRef,
    endRef: messagesEndRef,
    isAtBottom,
    scrollToBottom,
    hasSentMessage,
  } = useMessages({
    status,
  });

  const contentRef = useRef<HTMLDivElement>(null);
  // A ref, not state: this is read on every streamed chunk and every resize,
  // and must not trigger a re-render of the message list.
  const followRef = useRef(true);
  const compactions = useCompactions(chatId);
  const { chatWidthClass } = useAppConfig();

  useDataStream();

  useEffect(() => {
    if (status === 'submitted') {
      followRef.current = true;
      requestAnimationFrame(() => {
        const container = messagesContainerRef.current;
        if (container) {
          container.scrollTo({
            top: container.scrollHeight,
            behavior: 'smooth',
          });
        }
      });
    }
  }, [status, messagesContainerRef]);

  useEffect(() => {
    const container = messagesContainerRef.current;
    const content = contentRef.current;
    if (!container || !content) return;

    const release = () => {
      followRef.current = false;
    };

    const onWheel = (event: WheelEvent) => {
      if (event.deltaY < 0) release();
    };

    const onKeyDown = (event: KeyboardEvent) => {
      if (!RELEASE_KEYS.has(event.key)) return;
      const target = event.target as HTMLElement | null;
      // Caret movement in the composer is not a scroll gesture.
      if (target?.closest('input, textarea, [contenteditable="true"]')) return;
      release();
    };

    const onScroll = () => {
      const distance =
        container.scrollHeight - container.scrollTop - container.clientHeight;
      if (distance <= REARM_THRESHOLD_PX) followRef.current = true;
    };

    // Instant, never smooth: a smooth scroll re-issued on each chunk cancels
    // the previous animation, so the viewport permanently trails the text.
    const observer = new ResizeObserver(() => {
      if (followRef.current) container.scrollTop = container.scrollHeight;
    });

    // Content growth covers streaming and late-mounting lazy elements (charts,
    // tables); the container itself covers window resize and composer growth.
    observer.observe(content);
    observer.observe(container);

    container.addEventListener('wheel', onWheel, { passive: true });
    container.addEventListener('touchmove', release, { passive: true });
    container.addEventListener('scroll', onScroll, { passive: true });
    document.addEventListener('keydown', onKeyDown);

    return () => {
      observer.disconnect();
      container.removeEventListener('wheel', onWheel);
      container.removeEventListener('touchmove', release);
      container.removeEventListener('scroll', onScroll);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [messagesContainerRef]);

  return (
    <div
      ref={messagesContainerRef}
      className="overscroll-behavior-contain -webkit-overflow-scrolling-touch flex-1 touch-pan-y overflow-y-scroll"
      style={{ overflowAnchor: 'none' }}
    >
      {/* Layout-neutral wrapper (static, full-width block) that gives the
          ResizeObserver a handle on total content height. StickToBottom.Content
          overwrites any ref passed to it, so it cannot be observed directly. */}
      <div ref={contentRef}>
        <Conversation
          className={cn(
            'mx-auto flex min-w-0 flex-col gap-4 md:gap-6',
            chatWidthClass,
          )}
        >
          <ConversationContent className="flex flex-col gap-4 px-4 py-4 md:gap-6">
            {messages.map((message, index) => (
              <Fragment key={message.id}>
                {/* Agent Bricks: where a conversation summary starts (#637) */}
                <CompactionDivider compaction={compactions.get(message.id)} />
                <PreviewMessage
                  message={message}
                  allMessages={messages}
                  isLoading={
                    status === 'streaming' && messages.length - 1 === index
                  }
                  setMessages={setMessages}
                  addToolApprovalResponse={addToolApprovalResponse}
                  sendMessage={sendMessage}
                  regenerate={regenerate}
                  isReadonly={isReadonly}
                  requiresScrollPadding={
                    hasSentMessage && index === messages.length - 1
                  }
                  initialFeedback={feedback[message.id]}
                />
              </Fragment>
            ))}

            {status === 'submitted' &&
              messages.length > 0 &&
              messages[messages.length - 1].role === 'user' &&
              selectedModelId !== 'chat-model-reasoning' && (
                <AwaitingResponseMessage />
              )}

            <div
              ref={messagesEndRef}
              className="min-h-[24px] min-w-[24px] shrink-0"
            />
          </ConversationContent>
        </Conversation>
      </div>

      {!isAtBottom && (
        <button
          className="-translate-x-1/2 absolute bottom-40 left-1/2 z-10 rounded-full border bg-background p-2 shadow-lg transition-colors hover:bg-muted"
          onClick={() => scrollToBottom('smooth')}
          type="button"
          aria-label="Scroll to bottom"
        >
          <ArrowDownIcon className="size-4" />
        </button>
      )}
    </div>
  );
}

export const Messages = memo(PureMessages, (prevProps, nextProps) => {
  // Always re-render during streaming to ensure incremental token display
  if (prevProps.status === 'streaming' || nextProps.status === 'streaming') {
    return false;
  }

  if (prevProps.chatId !== nextProps.chatId) return false;
  if (prevProps.selectedModelId !== nextProps.selectedModelId) return false;
  if (prevProps.messages.length !== nextProps.messages.length) return false;
  if (!equal(prevProps.messages, nextProps.messages)) return false;
  if (!equal(prevProps.feedback, nextProps.feedback)) return false;

  return true; // Props are equal, skip re-render
});
