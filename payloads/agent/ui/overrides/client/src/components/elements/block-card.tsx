/**
 * Bordered card for fence-rendered blocks (reports, Mermaid diagrams): a
 * header with an icon, a label and actions, then the content. The header is
 * marked `data-snapshot-hide`, so a report's HTML download leaves it out.
 */
import { type ReactNode, forwardRef } from 'react';
import type { LucideIcon } from 'lucide-react';

interface BlockCardProps {
  icon: LucideIcon;
  label: string;
  actions?: ReactNode;
  children: ReactNode;
}

export const BlockCard = forwardRef<HTMLDivElement, BlockCardProps>(
  ({ icon: Icon, label, actions, children }, ref) => (
    <div ref={ref} className="my-2 rounded-md border border-border">
      <div
        data-snapshot-hide
        className="flex items-center justify-between gap-2 border-b border-border bg-muted/30 px-3 py-1.5"
      >
        <span className="inline-flex min-w-0 items-center gap-1.5 text-muted-foreground text-xs">
          <Icon className="h-3.5 w-3.5 shrink-0" />
          <span className="truncate">{label}</span>
        </span>
        <div className="flex items-center gap-1">{actions}</div>
      </div>
      <div className="px-4 py-3">{children}</div>
    </div>
  ),
);

BlockCard.displayName = 'BlockCard';

interface CardButtonProps {
  icon: LucideIcon;
  label: string;
  ariaLabel?: string;
  onClick: () => void;
}

/** A small icon-and-text button for a card header. */
export function CardButton({
  icon: Icon,
  label,
  ariaLabel,
  onClick,
}: CardButtonProps) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="inline-flex items-center gap-1 rounded px-2 py-1 text-muted-foreground text-xs hover:bg-muted"
      aria-label={ariaLabel ?? label}
    >
      <Icon className="h-3.5 w-3.5" />
      {label}
    </button>
  );
}
