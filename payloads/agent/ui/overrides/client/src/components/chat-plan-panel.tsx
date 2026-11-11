import { useState } from 'react';
import { Check, ChevronRight } from 'lucide-react';
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from '@/components/ui/collapsible';
import { type ChatPlan, type Milestone, planProgress } from '@/lib/chat-plan';
import { cn } from '@/lib/utils';

/**
 * Agent Bricks: the agent's plan above the chat input
 * (`spec.runtime.context.plan`).  Collapsed until the user opens it, so it
 * stays out of view while the agent works.
 */
export function ChatPlanPanel({ plan }: { plan: ChatPlan | null }) {
  const [open, setOpen] = useState(false);
  if (!plan) {
    return null;
  }
  return (
    <Collapsible
      open={open}
      onOpenChange={setOpen}
      className="w-full rounded-lg border bg-muted/30 px-3 py-2 text-sm"
    >
      <CollapsibleTrigger className="flex w-full items-center gap-1 text-left">
        <ChevronRight
          className={cn('h-4 w-4 shrink-0 transition-transform', open && 'rotate-90')}
        />
        <span className="truncate font-medium">{plan.goal || 'Plan'}</span>
        <span className="ml-auto shrink-0 text-muted-foreground text-xs">
          {planProgress(plan)}
        </span>
      </CollapsibleTrigger>
      <CollapsibleContent>
        <ol className="mt-2 flex flex-col gap-1">
          {plan.milestones.map((milestone, index) => (
            <MilestoneItem key={index} milestone={milestone} />
          ))}
        </ol>
      </CollapsibleContent>
    </Collapsible>
  );
}

function MilestoneItem({ milestone }: { milestone: Milestone }) {
  const finished = milestone.status !== 'open';
  return (
    <li
      className={cn(
        'flex items-start gap-2',
        milestone.status === 'dropped' && 'text-muted-foreground',
      )}
      title={milestone.note || undefined}
    >
      <span
        aria-hidden
        className="mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded-sm border"
      >
        {milestone.status === 'done' && <Check className="h-3 w-3" />}
      </span>
      <span className={cn(finished && 'line-through')}>
        <span className="sr-only">{`${milestone.status}: `}</span>
        {milestone.title}
      </span>
    </li>
  );
}
