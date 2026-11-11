/**
 * Agent Bricks: the chat input's context meter (#637).  Pure, so the
 * `node:test` harness can import it; only type imports.
 */
import type { ChatMessage, ContextWindowReading } from '@chat-template/core';
import type { Compaction } from '@/hooks/use-compactions';

/** From this share of the trigger on, the meter shows a warning colour. */
export const WARN_FRACTION = 0.8;
/** How long after a triggering turn the meter waits for its summary. */
export const COMPACTION_WATCH_MS = 120_000;

export type MeterState =
  | { kind: 'hidden' }
  | { kind: 'reading'; reading: ContextWindowReading; fraction: number; warn: boolean }
  | { kind: 'compacting' }
  | { kind: 'reset' };

/** The newest turn's reading, or `null` before the first measured turn. */
export function latestReading(messages: readonly ChatMessage[]): ContextWindowReading | null {
  for (const part of messages.flatMap((m) => m.parts).reverse()) {
    if (part.type === 'data-contextWindow') {
      return part.data;
    }
  }
  return null;
}

/** Creation time (epoch ms) of the chat's newest summary, or `null`. */
export function latestCompactionAt(compactions: ReadonlyMap<string, Compaction>): number | null {
  let latest: number | null = null;
  for (const { createdAt } of compactions.values()) {
    const at = Date.parse(createdAt);
    if (!Number.isNaN(at) && (latest === null || at > latest)) {
      latest = at;
    }
  }
  return latest;
}

/** Epoch ms until which a triggering reading waits for its summary, or `null`. */
export function compactionWatchUntil(reading: ContextWindowReading | null): number | null {
  return reading?.compacting ? Date.parse(reading.measuredAt) + COMPACTION_WATCH_MS : null;
}

export function meterState(
  reading: ContextWindowReading | null,
  compactedAt: number | null,
  nowMs: number,
): MeterState {
  if (!reading) {
    return { kind: 'hidden' };
  }
  if (compactedAt !== null && compactedAt >= Date.parse(reading.measuredAt)) {
    return { kind: 'reset' };
  }
  const watchUntil = compactionWatchUntil(reading);
  if (watchUntil !== null && nowMs < watchUntil) {
    return { kind: 'compacting' };
  }
  if (reading.estimatedInputTokens === null) {
    return { kind: 'hidden' };
  }
  const fraction = Math.min(1, reading.estimatedInputTokens / reading.triggerTokens);
  return { kind: 'reading', reading, fraction, warn: fraction >= WARN_FRACTION };
}

export function formatTokens(tokens: number): string {
  return tokens < 1_000 ? String(tokens) : `${Math.round(tokens / 1_000)}k`;
}

/** The meter's tooltip and accessible name. */
export function meterLabel(state: MeterState): string {
  switch (state.kind) {
    case 'hidden':
      return '';
    case 'compacting':
      return 'Summarizing earlier messages…';
    case 'reset':
      return 'Earlier messages summarized. Updates after your next message.';
    case 'reading': {
      const { estimatedInputTokens, triggerTokens } = state.reading;
      return `≈${formatTokens(estimatedInputTokens ?? 0)} of ${formatTokens(triggerTokens)} tokens. Earlier messages are summarized automatically at ${formatTokens(triggerTokens)}.`;
    }
  }
}
