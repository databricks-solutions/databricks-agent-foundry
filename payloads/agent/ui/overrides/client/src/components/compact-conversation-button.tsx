import { useState } from 'react';
import { useSWRConfig } from 'swr';
import { FoldVertical } from 'lucide-react';
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog';
import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/textarea';
import { toast } from '@/components/toast';
import { useAppConfig } from '@/contexts/AppConfigContext';
import {
  type CompactOutcome,
  compactConversation,
  compactionsKey,
} from '@/hooks/use-compactions';

/** Mirrors `MAX_FOCUS_CHARS` in the server's `lib/compaction.ts`. */
const MAX_FOCUS_CHARS = 500;

const OUTCOME_TOAST: Record<CompactOutcome, { type: 'success' | 'error'; description: string }> = {
  compacted: { type: 'success', description: 'Earlier messages were summarized for the assistant.' },
  nothing_to_compact: { type: 'success', description: 'The conversation is still short; nothing to summarize.' },
  busy: { type: 'error', description: 'A summary is already being written. Try again in a moment.' },
};

/**
 * Agent Bricks: "Compact conversation" in the chat header (#637), opt-in via
 * `compaction.manual_action`.  Asks for an optional focus, then summarizes
 * the older turns for the agent right away instead of at the trigger.
 */
export function CompactConversationButton({ chatId }: { chatId: string }) {
  const { compactionManualEnabled } = useAppConfig();
  const { mutate } = useSWRConfig();
  const [open, setOpen] = useState(false);
  const [focus, setFocus] = useState('');
  const [running, setRunning] = useState(false);

  if (!compactionManualEnabled) {
    return null;
  }

  const compact = async () => {
    setRunning(true);
    try {
      toast(OUTCOME_TOAST[await compactConversation(chatId, focus)]);
      setFocus('');
      await mutate(compactionsKey(chatId));
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      toast({ type: 'error', description: `Could not compact the conversation: ${message}` });
    } finally {
      setRunning(false);
      setOpen(false);
    }
  };

  return (
    <>
      <Button
        variant="tertiary"
        className="h-8 px-2"
        aria-label="Summarize earlier messages now"
        title="Summarize earlier messages now"
        disabled={running}
        onClick={() => setOpen(true)}
      >
        <FoldVertical />
      </Button>
      <AlertDialog open={open} onOpenChange={(next) => !running && setOpen(next)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Compact conversation</AlertDialogTitle>
            <AlertDialogDescription>
              Long conversations are summarized automatically. Compact now to
              summarize the earlier messages right away, optionally with a
              focus for what the summary keeps. You still see every message.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <Textarea
            value={focus}
            maxLength={MAX_FOCUS_CHARS}
            placeholder="Optional: what the summary should focus on"
            onChange={(event) => setFocus(event.target.value)}
          />
          <AlertDialogFooter>
            <AlertDialogCancel disabled={running}>Cancel</AlertDialogCancel>
            <AlertDialogAction
              disabled={running}
              onClick={(event) => {
                event.preventDefault();
                void compact();
              }}
            >
              {running ? 'Summarizing…' : 'Compact'}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}
