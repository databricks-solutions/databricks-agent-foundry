import { useCopyToClipboard } from 'usehooks-ts';

import { Actions, Action } from './elements/actions';
import { memo, useState, useCallback, useRef, useEffect } from 'react';
import { toast } from 'sonner';
import type { ChatMessage, Feedback } from '@chat-template/core';
import { useAppConfig } from '@/contexts/AppConfigContext';
import {
  ChevronDown,
  ChevronUp,
  MessageSquareText,
  Tags,
} from 'lucide-react';
import { DbIcon } from './ui/db-icon';
import {
  PencilIcon,
  CopyIcon,
  ThumbsUpIcon,
  ThumbsDownIcon,
} from './icons';
import { Badge } from './ui/badge';
import { Textarea } from './ui/textarea';
import { Button } from './ui/button';

const COMMENT_MAX_LENGTH = 1000;

function CommentPopover({
  messageId,
  onClose,
  onSubmitted,
}: {
  messageId: string;
  onClose: () => void;
  onSubmitted: () => void;
}) {
  const [text, setText] = useState('');
  const [isSubmitting, setIsSubmitting] = useState(false);

  const handleSubmit = useCallback(async () => {
    const trimmed = text.trim();
    if (!trimmed || isSubmitting) return;
    setIsSubmitting(true);
    try {
      const response = await fetch('/api/feedback', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          messageId,
          feedbackType: 'comment',
          comment: trimmed,
        }),
      });
      if (!response.ok) throw new Error('Failed to submit comment');
      onSubmitted();
      onClose();
    } catch {
      toast.error('Failed to submit comment. Please try again.');
    } finally {
      setIsSubmitting(false);
    }
  }, [text, messageId, isSubmitting, onSubmitted, onClose]);

  return (
    <div className="mt-2 flex w-full max-w-md flex-col gap-2 rounded-lg border bg-background p-3 shadow-sm">
      <Textarea
        value={text}
        onChange={(e) => setText(e.target.value.slice(0, COMMENT_MAX_LENGTH))}
        placeholder="Leave a comment..."
        rows={2}
        className="resize-none text-sm"
        autoFocus
      />
      <div className="flex items-center justify-between">
        <span className="text-xs text-muted-foreground">
          {text.length}/{COMMENT_MAX_LENGTH}
        </span>
        <div className="flex gap-2">
          <Button variant="ghost" size="sm" onClick={onClose}>
            Cancel
          </Button>
          <Button
            size="sm"
            onClick={handleSubmit}
            disabled={!text.trim() || isSubmitting}
          >
            Submit
          </Button>
        </div>
      </div>
    </div>
  );
}

function CategoryPopover({
  messageId,
  categories,
  onClose,
  onSubmitted,
}: {
  messageId: string;
  categories: Array<{ id: string; label: string }>;
  onClose: () => void;
  onSubmitted: () => void;
}) {
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [isSubmitting, setIsSubmitting] = useState(false);

  const toggle = useCallback((id: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const handleSubmit = useCallback(async () => {
    if (selected.size === 0 || isSubmitting) return;
    setIsSubmitting(true);
    try {
      const response = await fetch('/api/feedback', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          messageId,
          feedbackType: 'categories',
          categories: Array.from(selected),
        }),
      });
      if (!response.ok) throw new Error('Failed to submit categories');
      onSubmitted();
      onClose();
    } catch {
      toast.error('Failed to submit feedback. Please try again.');
    } finally {
      setIsSubmitting(false);
    }
  }, [selected, messageId, isSubmitting, onSubmitted, onClose]);

  return (
    <div className="mt-2 flex w-full max-w-md flex-col gap-2 rounded-lg border bg-background p-3 shadow-sm">
      <div className="flex flex-wrap gap-1.5">
        {categories.map((cat) => (
          <Badge
            key={cat.id}
            variant={selected.has(cat.id) ? 'default' : 'outline'}
            className="cursor-pointer select-none"
            onClick={() => toggle(cat.id)}
          >
            {cat.label}
          </Badge>
        ))}
      </div>
      <div className="flex justify-end gap-2">
        <Button variant="ghost" size="sm" onClick={onClose}>
          Cancel
        </Button>
        <Button
          size="sm"
          onClick={handleSubmit}
          disabled={selected.size === 0 || isSubmitting}
        >
          Submit
        </Button>
      </div>
    </div>
  );
}

function PureMessageActions({
  message,
  isLoading,
  setMode,
  errorCount = 0,
  showErrors = false,
  onToggleErrors,
  initialFeedback,
}: {
  message: ChatMessage;
  isLoading: boolean;
  setMode?: (mode: 'view' | 'edit') => void;
  errorCount?: number;
  showErrors?: boolean;
  onToggleErrors?: () => void;
  initialFeedback?: Feedback;
}) {
  const { feedbackEnabled, feedbackCategories } = useAppConfig();
  const [_, copyToClipboard] = useCopyToClipboard();
  const [feedback, setFeedback] = useState<'thumbs_up' | 'thumbs_down' | null>(
    initialFeedback?.feedbackType || null,
  );
  const isSubmittingRef = useRef(false);
  const [activePopover, setActivePopover] = useState<
    'comment' | 'categories' | null
  >(null);
  const [commentSubmitted, setCommentSubmitted] = useState(false);
  const [categoriesSubmitted, setCategoriesSubmitted] = useState(false);

  useEffect(() => {
    if (initialFeedback?.feedbackType && feedback === null) {
      setFeedback(initialFeedback.feedbackType);
    }
  }, [initialFeedback?.feedbackType]);

  const textFromParts = message.parts
    ?.filter((part) => part.type === 'text')
    .map((part) => part.text)
    .join('\n')
    .trim();

  const traceIdPart = message.parts?.find((p) => p.type === 'data-traceId') as
    | { type: 'data-traceId'; data: string | null }
    | undefined;
  const feedbackSupported =
    traceIdPart === undefined || traceIdPart.data !== null;

  const handleFeedback = useCallback(
    async (feedbackType: 'thumbs_up' | 'thumbs_down') => {
      if (isSubmittingRef.current) return;
      isSubmittingRef.current = true;

      try {
        const response = await fetch('/api/feedback', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ messageId: message.id, feedbackType }),
        });

        if (!response.ok) throw new Error('Failed to submit feedback');
        setFeedback(feedbackType);
      } catch {
        toast.error(
          'Failed to submit feedback. Please try again, or contact the app developer if the error persists.',
        );
      } finally {
        isSubmittingRef.current = false;
      }
    },
    [message.id],
  );

  const handleCopy = useCallback(async () => {
    if (!textFromParts) {
      toast.error("There's no text to copy!");
      return;
    }
    await copyToClipboard(textFromParts);
    toast.success('Copied to clipboard!');
  }, [textFromParts, copyToClipboard]);

  if (isLoading) return null;

  if (message.role === 'user') {
    return (
      <Actions className="-mr-0.5 justify-end">
        <div className="relative flex items-center gap-1">
          {setMode && (
            <Action
              tooltip="Edit"
              onClick={() => setMode('edit')}
              className="opacity-0 transition-opacity group-hover/message:opacity-100"
              data-testid="message-edit-button"
            >
              <DbIcon icon={PencilIcon} />
            </Action>
          )}
          <Action tooltip="Copy" onClick={handleCopy}>
            <DbIcon icon={CopyIcon} />
          </Action>
        </div>
      </Actions>
    );
  }

  const showExtendedFeedback =
    feedbackEnabled && feedbackSupported && feedbackCategories.length > 0;

  return (
    <div className="flex flex-col">
      <Actions className="-ml-0.5">
        {textFromParts && (
          <Action tooltip="Copy" onClick={handleCopy}>
            <CopyIcon />
          </Action>
        )}

        {feedbackEnabled && feedbackSupported && (
          <>
            <Action
              tooltip="Thumbs up"
              onClick={() => handleFeedback('thumbs_up')}
              className={feedback === 'thumbs_up' ? 'text-green-600' : ''}
              data-testid="thumbs-up-button"
            >
              <DbIcon icon={ThumbsUpIcon} />
            </Action>
            <Action
              tooltip="Thumbs down"
              onClick={() => handleFeedback('thumbs_down')}
              className={feedback === 'thumbs_down' ? 'text-red-600' : ''}
              data-testid="thumbs-down-button"
            >
              <DbIcon icon={ThumbsDownIcon} />
            </Action>
          </>
        )}

        {showExtendedFeedback && (
          <>
            <Action
              tooltip="Leave a comment"
              onClick={() =>
                setActivePopover((p) => (p === 'comment' ? null : 'comment'))
              }
              className={commentSubmitted ? 'text-blue-600' : ''}
              data-testid="comment-button"
            >
              <MessageSquareText size={16} />
            </Action>
            <Action
              tooltip="Categorize response"
              onClick={() =>
                setActivePopover((p) =>
                  p === 'categories' ? null : 'categories',
                )
              }
              className={categoriesSubmitted ? 'text-blue-600' : ''}
              data-testid="categories-button"
            >
              <Tags size={16} />
            </Action>
          </>
        )}

        {errorCount > 0 && onToggleErrors && (
          <Action
            tooltip={showErrors ? 'Hide errors' : 'Show errors'}
            onClick={onToggleErrors}
            iconOnly={false}
          >
            <div className="flex items-center gap-1.5">
              {showErrors ? <ChevronUp /> : <ChevronDown />}
              <span className="text-xs">
                {errorCount} {errorCount === 1 ? 'error' : 'errors'}
              </span>
            </div>
          </Action>
        )}
      </Actions>

      {activePopover === 'comment' && (
        <CommentPopover
          messageId={message.id}
          onClose={() => setActivePopover(null)}
          onSubmitted={() => setCommentSubmitted(true)}
        />
      )}

      {activePopover === 'categories' && (
        <CategoryPopover
          messageId={message.id}
          categories={feedbackCategories}
          onClose={() => setActivePopover(null)}
          onSubmitted={() => setCategoriesSubmitted(true)}
        />
      )}
    </div>
  );
}

export const MessageActions = memo(
  PureMessageActions,
  (prevProps, nextProps) => {
    if (prevProps.isLoading !== nextProps.isLoading) return false;
    if (prevProps.errorCount !== nextProps.errorCount) return false;
    if (prevProps.showErrors !== nextProps.showErrors) return false;
    if (
      prevProps.initialFeedback?.feedbackType !==
      nextProps.initialFeedback?.feedbackType
    )
      return false;
    const prevTraceId = prevProps.message.parts?.find(
      (p) => p.type === 'data-traceId',
    );
    const nextTraceId = nextProps.message.parts?.find(
      (p) => p.type === 'data-traceId',
    );
    if (prevTraceId !== nextTraceId) return false;

    return true;
  },
);
