import type { Compaction } from '@/hooks/use-compactions';

/**
 * Agent Bricks: marks where a conversation summary starts (#637).  The
 * messages above stay visible; the agent sees only the summary in their
 * place from here on.
 */
export function CompactionDivider({ compaction }: { compaction: Compaction | undefined }) {
  if (!compaction) {
    return null;
  }
  return (
    <details className="group text-muted-foreground text-xs">
      <summary className="flex cursor-pointer list-none items-center gap-3">
        <span className="h-px flex-1 bg-border" />
        <span>Earlier messages were summarized for the assistant · show summary</span>
        <span className="h-px flex-1 bg-border" />
      </summary>
      <p className="mt-2 whitespace-pre-wrap rounded-md border border-border bg-muted p-3 text-foreground text-sm">
        {compaction.summary}
      </p>
    </details>
  );
}
