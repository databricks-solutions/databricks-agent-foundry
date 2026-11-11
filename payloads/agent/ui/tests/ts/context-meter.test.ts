import { strict as assert } from 'node:assert';
import { describe, it } from 'node:test';
import type { ChatMessage, ContextWindowReading } from '@chat-template/core';
import type { Compaction } from '../../overrides/client/src/hooks/use-compactions.ts';
import {
  COMPACTION_WATCH_MS,
  formatTokens,
  latestCompactionAt,
  latestReading,
  meterLabel,
  meterState,
} from '../../overrides/client/src/lib/context-meter.ts';

// Type-only imports are erased by Node's TS stripping.

const MEASURED_AT = '2026-10-27T12:00:00.000Z';
const MEASURED_MS = Date.parse(MEASURED_AT);

function reading(overrides: Partial<ContextWindowReading> = {}): ContextWindowReading {
  return { estimatedInputTokens: 40_000, triggerTokens: 100_000, compacting: false, measuredAt: MEASURED_AT, ...overrides };
}

function message(id: string, parts: Array<Record<string, unknown>>): ChatMessage {
  return { id, role: 'assistant', parts } as unknown as ChatMessage;
}

function compaction(createdAt: string): Compaction {
  return { id: createdAt, boundaryMessageId: createdAt, summary: '', trigger: 'auto', createdAt };
}

describe('latestReading', () => {
  it('returns the newest reading across messages', () => {
    const older = reading({ estimatedInputTokens: 1 });
    const newer = reading({ estimatedInputTokens: 2 });
    const messages = [
      message('a', [{ type: 'data-contextWindow', data: older }]),
      message('b', [{ type: 'text', text: 'hi' }, { type: 'data-contextWindow', data: newer }]),
      message('c', [{ type: 'text', text: 'no reading' }]),
    ];
    assert.equal(latestReading(messages), newer);
  });

  it('is null before the first measured turn', () => {
    assert.equal(latestReading([message('a', [{ type: 'text', text: 'hi' }])]), null);
  });
});

describe('latestCompactionAt', () => {
  it('picks the newest summary and skips bad dates', () => {
    const map = new Map([
      ['a', compaction('2026-10-27T11:00:00.000Z')],
      ['b', compaction('2026-10-27T13:00:00.000Z')],
      ['c', compaction('not a date')],
    ]);
    assert.equal(latestCompactionAt(map), Date.parse('2026-10-27T13:00:00.000Z'));
    assert.equal(latestCompactionAt(new Map()), null);
  });
});

describe('meterState', () => {
  it('is hidden without a reading', () => {
    assert.deepEqual(meterState(null, null, MEASURED_MS), { kind: 'hidden' });
  });

  it('shows the share of the trigger, warning from 80% and capped at full', () => {
    const below = meterState(reading(), null, MEASURED_MS);
    assert.deepEqual(below, { kind: 'reading', reading: reading(), fraction: 0.4, warn: false });
    const warn = meterState(reading({ estimatedInputTokens: 80_000 }), null, MEASURED_MS);
    assert.equal(warn.kind === 'reading' && warn.warn, true);
    const over = meterState(reading({ estimatedInputTokens: 150_000 }), null, MEASURED_MS);
    assert.equal(over.kind === 'reading' && over.fraction, 1);
  });

  it('is compacting while the summary is awaited, then falls back to the reading', () => {
    const triggering = reading({ estimatedInputTokens: 100_000, compacting: true });
    assert.equal(meterState(triggering, null, MEASURED_MS + 1_000).kind, 'compacting');
    assert.equal(meterState(triggering, null, MEASURED_MS + COMPACTION_WATCH_MS).kind, 'reading');
  });

  it('shows a rejected-for-length turn as compacting', () => {
    const rejected = reading({ estimatedInputTokens: null, compacting: true });
    assert.equal(meterState(rejected, null, MEASURED_MS).kind, 'compacting');
  });

  it('resets once a summary is newer than the reading', () => {
    const triggering = reading({ compacting: true });
    assert.equal(meterState(triggering, MEASURED_MS + 5_000, MEASURED_MS + 6_000).kind, 'reset');
    assert.equal(meterState(reading(), MEASURED_MS - 1, MEASURED_MS).kind, 'reading');
  });
});

describe('labels', () => {
  it('formats tokens compactly', () => {
    assert.equal(formatTokens(950), '950');
    assert.equal(formatTokens(42_400), '42k');
  });

  it('states the reading against the trigger', () => {
    assert.equal(
      meterLabel(meterState(reading(), null, MEASURED_MS)),
      '≈40k of 100k tokens. Earlier messages are summarized automatically at 100k.',
    );
    assert.equal(meterLabel({ kind: 'compacting' }), 'Summarizing earlier messages…');
  });
});
