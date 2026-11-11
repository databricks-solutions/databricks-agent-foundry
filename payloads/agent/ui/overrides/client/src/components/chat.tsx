import type { DataUIPart, LanguageModelUsage, UIMessageChunk } from 'ai';
import { useChat } from '@ai-sdk/react';
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { useSWRConfig } from 'swr';
import { ChatHeader } from '@/components/chat-header';
import { cn, fetchWithErrorHandlers, generateUUID } from '@/lib/utils';
import { MultimodalInput } from './multimodal-input';
import { Messages } from './messages';
import type {
  Attachment,
  ChatMessage,
  CustomUIDataTypes,
  FeedbackMap,
  UserSkillSaved,
  VisibilityType,
} from '@chat-template/core';
import { unstable_serialize } from 'swr/infinite';
import { getChatHistoryPaginationKey } from './sidebar-history';
import { toast } from './toast';
import { useSearchParams } from 'react-router-dom';
import { useChatVisibility } from '@/hooks/use-chat-visibility';
import { ChatSDKError } from '@chat-template/core/errors';
import { useDataStream } from './data-stream-provider';
import { isCredentialErrorMessage } from '@/lib/oauth-error-utils';
import { WebSocketChatTransport } from '../lib/websocket-chat-transport';
import { requestTurnStop } from '../lib/stop-chat-turn';
import type { ClientSession } from '@chat-template/auth';
import { softNavigateToChatId } from '@/lib/navigation';
import { useAppConfig } from '@/contexts/AppConfigContext';
import { Greeting } from './greeting';
import { chatUiExtensions } from '@/extensions/registry';
import type { ChatEffect } from '@/extensions/types';
import { refreshUserSkillData } from '@/hooks/use-resource-picks';
import { skillSavedText } from '@/lib/skill-draft';
import { compactionsKey } from '@/hooks/use-compactions';
import { useChatPlan } from '@/hooks/use-chat-plan';
import { ChatPlanPanel } from './chat-plan-panel';

const noChatEffect: ChatEffect = () => {};

function announceSkillSaved(
  saved: UserSkillSaved,
  mutate: (key: string) => Promise<unknown>,
): void {
  toast({ type: saved.ok ? 'success' : 'error', description: skillSavedText(saved) });
  if (saved.ok) {
    void refreshUserSkillData(mutate);
  }
}

export function Chat({
  id,
  initialMessages,
  initialChatModel,
  initialVisibilityType,
  isReadonly,
  initialLastContext,
  feedback = {},
  title,
  pinnedPanel,
}: {
  id: string;
  initialMessages: ChatMessage[];
  initialChatModel: string;
  initialVisibilityType: VisibilityType;
  isReadonly: boolean;
  session: ClientSession;
  initialLastContext?: LanguageModelUsage;
  feedback?: FeedbackMap;
  title?: string;
  /** Pinned between the header and the conversation (extension routes). */
  pinnedPanel?: ReactNode;
}) {
  const { visibilityType } = useChatVisibility({
    chatId: id,
    initialVisibilityType,
  });

  const { mutate } = useSWRConfig();
  const { setDataStream } = useDataStream();
  const { chatHistoryEnabled, chatWidthClass } = useAppConfig();

  const [input, setInput] = useState<string>('');
  const [_usage, setUsage] = useState<LanguageModelUsage | undefined>(
    initialLastContext,
  );

  // Written in onStreamPart, not mirrored from state: onFinish runs before the render that would record the final part.
  const lastPartRef = useRef<UIMessageChunk | undefined>(undefined);

  // Single counter for resume attempts - reset when stream parts are received
  const resumeAttemptCountRef = useRef(0);
  const maxResumeAttempts = 3;

  const abortController = useRef<AbortController | null>(new AbortController());
  useEffect(() => {
    return () => {
      abortController.current?.abort('ABORT_SIGNAL');
    };
  }, []);

  const fetchWithAbort = useMemo(() => {
    return async (input: RequestInfo | URL, init?: RequestInit) => {
      // useChat does not cancel /stream requests when the component is unmounted
      const signal = abortController.current?.signal;
      // WebKit's HTTP-cache lock can stall streamed responses; bypass the cache.
      return fetchWithErrorHandlers(input, { ...init, signal, cache: 'no-store' });
    };
  }, []);

  // The running turn's server id, when the WebSocket transport reported it.
  const turnStreamIdRef = useRef<string | undefined>(undefined);
  // The SDK's isAbort misses a stop over SSE: the fetch rejects with our string reason, not an AbortError.
  const userStoppedRef = useRef(false);
  // The pinned SDK calls onFinish after a resume that found no running turn; there is nothing to resume then.
  const noActiveTurnRef = useRef(false);
  const stop = useCallback(() => {
    userStoppedRef.current = true;
    abortController.current?.abort('USER_ABORT_SIGNAL');
    // An aborted signal stays aborted; without a fresh one every later request is cancelled at once.
    abortController.current = new AbortController();
    requestTurnStop(id, turnStreamIdRef.current);
  }, [id]);

  const isNewChat = initialMessages.length === 0;
  const didFetchHistoryOnNewChat = useRef(false);
  const fetchChatHistory = useCallback(() => {
    mutate(unstable_serialize(getChatHistoryPaginationKey));
  }, [mutate]);

  // For new chats, the title arrives via a `data-title` stream part
  // once backend title generation completes — no separate fetch needed.
  const [streamTitle, setStreamTitle] = useState<string | undefined>();
  const [titlePending, setTitlePending] = useState(false);
  const displayTitle = title ?? streamTitle;

  const {
    messages,
    setMessages,
    sendMessage,
    status,
    resumeStream,
    clearError,
    addToolApprovalResponse,
    regenerate,
  } = useChat<ChatMessage>({
    id,
    messages: initialMessages,
    experimental_throttle: 100,
    generateId: generateUUID,
    resume: id !== undefined && initialMessages.length > 0, // Enable automatic stream resumption
    transport: new WebSocketChatTransport({
      signal: () => abortController.current?.signal,
      onTurnStart: (streamId) => {
        turnStreamIdRef.current = streamId;
      },
      onNoActiveTurn: () => {
        noActiveTurnRef.current = true;
      },
      onStreamPart: (part) => {
        noActiveTurnRef.current = false;
        if (isNewChat && !didFetchHistoryOnNewChat.current) {
          fetchChatHistory();
          if (chatHistoryEnabled) {
            setTitlePending(true);
          }
          didFetchHistoryOnNewChat.current = true;
        }
        // Reset resume attempts when we successfully receive stream parts
        resumeAttemptCountRef.current = 0;
        lastPartRef.current = part;
      },
      api: '/api/chat',
      fetch: fetchWithAbort,
      prepareSendMessagesRequest({ messages, id, body }) {
        const lastMessage = messages.at(-1);
        const isUserMessage = lastMessage?.role === 'user';

        // For continuations (non-user messages like tool results), we must always
        // send previousMessages because the tool result only exists client-side
        // and hasn't been saved to the database yet.
        const needsPreviousMessages = !chatHistoryEnabled || !isUserMessage;

        return {
          body: {
            id,
            // Only include message field for user messages (new messages)
            // For continuation (assistant messages with tool results), omit message field
            ...(isUserMessage ? { message: lastMessage } : {}),
            selectedChatModel: initialChatModel,
            selectedVisibilityType: visibilityType,
            nextMessageId: generateUUID(),
            // Send previous messages when:
            // 1. Database is disabled (ephemeral mode) - always need client-side messages
            // 2. Continuation request (tool results) - tool result only exists client-side
            ...(needsPreviousMessages
              ? {
                  previousMessages: isUserMessage
                    ? messages.slice(0, -1)
                    : messages,
                }
              : {}),
            ...body,
          },
        };
      },
      prepareReconnectToStreamRequest({ id }) {
        return {
          api: `/api/chat/${id}/stream`,
          credentials: 'include',
        };
      },
    }),
    onData: (dataPart) => {
      setDataStream((ds) =>
        ds ? [...ds, dataPart as DataUIPart<CustomUIDataTypes>] : [],
      );
      if (dataPart.type === 'data-usage') {
        setUsage(dataPart.data as LanguageModelUsage);
      }
      if (dataPart.type === 'data-title') {
        setStreamTitle(dataPart.data as string);
        setTitlePending(false);
        fetchChatHistory();
      }
      // Agent Bricks: a skill the agent saved for the user (#642)
      if (dataPart.type === 'data-userSkillSaved') {
        announceSkillSaved(dataPart.data as UserSkillSaved, mutate);
      }
    },
    onFinish: ({
      isAbort,
      messages: finishedMessages,
    }) => {
      turnStreamIdRef.current = undefined;
      const stopped = isAbort || userStoppedRef.current;
      userStoppedRef.current = false;
      const noActiveTurn = noActiveTurnRef.current;
      noActiveTurnRef.current = false;
      didFetchHistoryOnNewChat.current = false;
      setTitlePending(false);

      // If user aborted, don't try to resume
      if (stopped) {
        console.log('[Chat onFinish] Stream was aborted by user, not resuming');
        fetchChatHistory();
        return;
      }

      // Check if the last message contains an OAuth credential error
      // If so, don't try to resume - the user needs to authenticate first
      const lastMessage = finishedMessages?.at(-1);
      const hasOAuthError = lastMessage?.parts?.some(
        (part) =>
          part.type === 'data-error' &&
          typeof part.data === 'string' &&
          isCredentialErrorMessage(part.data),
      );

      if (hasOAuthError) {
        console.log(
          '[Chat onFinish] OAuth credential error detected, not resuming',
        );
        fetchChatHistory();
        clearError();
        return;
      }

      // Determine if we should attempt to resume:
      // 1. Stream didn't end with a 'finish' part (incomplete). A connection
      //    can close cleanly mid-turn, without a disconnect or error.
      // 2. We haven't exceeded max resume attempts
      // 3. The resume that just ended found a running turn
      const shouldResume =
        !noActiveTurn && lastPartRef.current?.type !== 'finish';

      if (shouldResume && resumeAttemptCountRef.current < maxResumeAttempts) {
        console.log(
          '[Chat onFinish] Resuming stream. Attempt:',
          resumeAttemptCountRef.current + 1,
        );
        resumeAttemptCountRef.current++;
        // Ref: https://github.com/vercel/ai/issues/8477#issuecomment-3603209884
        queueMicrotask(() => {
          resumeStream();
        })
      } else {
        // Stream completed normally or we've exhausted resume attempts
        if (resumeAttemptCountRef.current >= maxResumeAttempts) {
          console.warn('[Chat onFinish] Max resume attempts reached');
        }
        fetchChatHistory();
        // Agent Bricks: picks up a summary written after an earlier turn (#637)
        void mutate(compactionsKey(id));
      }
    },
    onError: (error) => {
      console.log('[Chat onError] Error occurred:', error);

      // Only show toast for explicit ChatSDKError (backend validation errors)
      // Other errors (network, schema validation) are handled silently or in message parts
      if (error instanceof ChatSDKError) {
        toast({
          type: 'error',
          description: error.message,
        });
      } else {
        // Non-ChatSDKError: Could be network error or in-stream error
        // Log but don't toast - errors during streaming may be informational
        console.warn('[Chat onError] Error during streaming:', error.message);
      }
      // Note: We don't call resumeStream here because onError can be called
      // while the stream is still active (e.g., for data-error parts).
      // Resume logic is handled exclusively in onFinish.
    },
  });

  const useChatEffect = chatUiExtensions.useChatEffect ?? noChatEffect;
  useChatEffect(id, messages);

  const [searchParams] = useSearchParams();
  const query = searchParams.get('query');

  const [hasAppendedQuery, setHasAppendedQuery] = useState(false);

  useEffect(() => {
    if (query && !hasAppendedQuery) {
      sendMessage({
        role: 'user' as const,
        parts: [{ type: 'text', text: query }],
      });

      setHasAppendedQuery(true);
      softNavigateToChatId(id, chatHistoryEnabled);
    }
  }, [query, sendMessage, hasAppendedQuery, id, chatHistoryEnabled]);

  const [attachments, setAttachments] = useState<Array<Attachment>>([]);
  const plan = useChatPlan(id, messages);

  const inputElement = <MultimodalInput
    chatId={id}
    input={input}
    setInput={setInput}
    status={status}
    stop={stop}
    attachments={attachments}
    setAttachments={setAttachments}
    messages={messages}
    setMessages={setMessages}
    sendMessage={sendMessage}
    selectedVisibilityType={visibilityType}
  />

  if (messages.length === 0) {
    return (
      <div className="flex h-dvh min-w-0 flex-col bg-background">
        <ChatHeader empty />
        {pinnedPanel}
        <div className="flex min-h-0 flex-1 overflow-y-auto overscroll-contain touch-pan-y p-4">
          <div className={cn('m-auto flex w-full flex-col', chatWidthClass)}>
            <Greeting />
            {inputElement}
          </div>
        </div>
      </div>
    );
  }

  return (
    <>
      <div className="overscroll-behavior-contain flex h-dvh min-w-0 touch-pan-y flex-col bg-background">
        <ChatHeader
          title={displayTitle}
          isLoadingTitle={titlePending && !displayTitle}
          compactChatId={isReadonly ? undefined : id}
        />
        {pinnedPanel}

        <Messages
          chatId={id}
          status={status}
          messages={messages}
          setMessages={setMessages}
          addToolApprovalResponse={addToolApprovalResponse}
          regenerate={regenerate}
          sendMessage={sendMessage}
          isReadonly={isReadonly}
          selectedModelId={initialChatModel}
          feedback={feedback}
        />



        <div
          className={cn(
            'sticky bottom-0 z-1 mx-auto flex w-full gap-2 border-t-0 bg-background px-2 pb-3 md:px-4 md:pb-4',
            chatWidthClass,
          )}
        >
          {!isReadonly && (
            <div className="flex w-full min-w-0 flex-col gap-2">
              <ChatPlanPanel plan={plan} />
              {inputElement}
            </div>
          )}
        </div>
      </div>
    </>
  );
}
