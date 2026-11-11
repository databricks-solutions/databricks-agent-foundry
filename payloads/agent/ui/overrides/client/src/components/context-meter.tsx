import { useEffect, useMemo, useRef } from 'react';
import type { ChatMessage } from '@chat-template/core';
import { toast } from '@/components/toast';
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from '@/components/ui/tooltip';
import { useCompactions } from '@/hooks/use-compactions';
import {
  compactionWatchUntil,
  latestCompactionAt,
  latestReading,
  meterLabel,
  meterState,
  type MeterState,
} from '@/lib/context-meter';
import { cn } from '@/lib/utils';

const RADIUS = 6;
const CIRCUMFERENCE = 2 * Math.PI * RADIUS;

const TRANSITION_TOAST: Partial<Record<MeterState['kind'], Partial<Record<MeterState['kind'], string>>>> = {
  reading: { compacting: 'Summarizing earlier messages…' },
  compacting: { reset: 'Earlier messages summarized' },
};

function filledFraction(state: MeterState): number {
  return state.kind === 'reading' ? state.fraction : state.kind === 'compacting' ? 1 : 0;
}

/**
 * Agent Bricks: how full the chat's context is, against the compaction
 * trigger (#637).  Toasts while the older turns are summarized, then resets.
 */
export function ContextMeter({ chatId, messages }: { chatId: string; messages: ChatMessage[] }) {
  const reading = useMemo(() => latestReading(messages), [messages]);
  const compactions = useCompactions(chatId, compactionWatchUntil(reading));
  const state = meterState(reading, latestCompactionAt(compactions), Date.now());
  const previousKind = useRef(state.kind);

  useEffect(() => {
    const description = TRANSITION_TOAST[previousKind.current]?.[state.kind];
    previousKind.current = state.kind;
    if (description) {
      toast({ type: 'success', description });
    }
  }, [state.kind]);

  if (state.kind === 'hidden') {
    return null;
  }
  const label = meterLabel(state);
  const warn = state.kind === 'reading' && state.warn;
  return (
    <TooltipProvider>
      <Tooltip>
        <TooltipTrigger asChild>
          <span
            role="img"
            aria-label={label}
            data-testid="context-meter"
            className={cn(
              'inline-flex size-8 items-center justify-center text-muted-foreground',
              warn && 'text-amber-600 dark:text-amber-400',
              state.kind === 'compacting' && 'animate-pulse',
            )}
          >
            <svg viewBox="0 0 16 16" className="size-4 -rotate-90" aria-hidden="true">
              <circle cx="8" cy="8" r={RADIUS} fill="none" stroke="currentColor" strokeOpacity={0.25} strokeWidth={2} />
              <circle
                cx="8"
                cy="8"
                r={RADIUS}
                fill="none"
                stroke="currentColor"
                strokeWidth={2}
                strokeDasharray={CIRCUMFERENCE}
                strokeDashoffset={CIRCUMFERENCE * (1 - filledFraction(state))}
              />
            </svg>
          </span>
        </TooltipTrigger>
        <TooltipContent>{label}</TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}
