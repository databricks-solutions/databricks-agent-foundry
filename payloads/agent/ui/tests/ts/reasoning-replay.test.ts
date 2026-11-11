import { strict as assert } from 'node:assert';
import { describe, it } from 'node:test';
import type { ChatMessage } from '@chat-template/core';
import { customOutputFromRaw } from '../../overrides/server/src/lib/compaction.ts';
import {
  REASONING_REPLAY_KEY,
  entriesForRequest,
  isReplayEntry,
  replayEntriesFrom,
  type ReplayEntry,
} from '../../overrides/server/src/lib/reasoning-replay.ts';
import { toolCallIds } from '../../overrides/server/src/lib/sanitize-tool-pairs.ts';
import {
  encodeHeaderJson,
  withReasoningReplay,
} from '../../overrides/packages/ai-sdk-providers/src/selected-resources.ts';

// Messages are plain objects cast through `unknown`; the `@chat-template/core`
// import is type-only and erased by Node's TS stripping.

function entry(...ids: string[]): ReplayEntry {
  return {
    model: 'databricks-claude-sonnet-5-5',
    tool_call_ids: ids,
    content: [{ type: 'reasoning', summary: [{ type: 'summary_text', text: 'x', signature: 's' }] }],
  };
}

function message(parts: Record<string, unknown>[]): ChatMessage {
  return { id: 'm', role: 'assistant', parts } as unknown as ChatMessage;
}

describe('isReplayEntry', () => {
  it('accepts the agent shape', () => {
    assert.equal(isReplayEntry(entry('toolu_1')), true);
  });

  it('accepts a grouping-only entry with empty content', () => {
    assert.equal(isReplayEntry({ ...entry('toolu_1', 'toolu_2'), content: [] }), true);
  });

  for (const [name, value] of [
    ['null', null],
    ['no model', { ...entry('toolu_1'), model: '' }],
    ['no tool calls', entry()],
    ['a non-string id', { ...entry('toolu_1'), tool_call_ids: [1] }],
    ['no content', { model: 'm', tool_call_ids: ['toolu_1'] }],
    ['a non-object block', { ...entry('toolu_1'), content: ['text'] }],
  ] as const) {
    it(`rejects ${name}`, () => {
      assert.equal(isReplayEntry(value), false);
    });
  }
});

describe('replayEntriesFrom', () => {
  it('reads the entries from the completed event and drops malformed ones', () => {
    const raw = {
      type: 'response.completed',
      custom_outputs: { [REASONING_REPLAY_KEY]: [entry('toolu_1'), { model: 'm' }] },
    };

    assert.deepEqual(replayEntriesFrom(customOutputFromRaw(raw, REASONING_REPLAY_KEY)), [entry('toolu_1')]);
  });

  it('is null when the event carries none', () => {
    const raw = { type: 'response.completed', custom_outputs: { context_window: {} } };

    assert.equal(replayEntriesFrom(customOutputFromRaw(raw, REASONING_REPLAY_KEY)), null);
    assert.equal(replayEntriesFrom(customOutputFromRaw({ type: 'response.output_item.done' }, REASONING_REPLAY_KEY)), null);
  });
});

describe('entriesForRequest', () => {
  it('keeps only entries whose tool calls are still in the request, in order', () => {
    const messages = [
      message([
        { type: 'text', text: 'Checking.' },
        { type: 'tool-lookup', toolCallId: 'toolu_2', state: 'output-available' },
        { type: 'dynamic-tool', toolName: 'q', toolCallId: 'toolu_3', state: 'output-available' },
      ]),
    ];
    const stored = [entry('toolu_1'), entry('toolu_2'), entry('toolu_3', 'toolu_4')];

    assert.deepEqual(entriesForRequest(stored, toolCallIds(messages)), [entry('toolu_2'), entry('toolu_3', 'toolu_4')]);
  });

  it('is empty without tool calls, which still opts the agent in', () => {
    assert.deepEqual(entriesForRequest([entry('toolu_1')], toolCallIds([message([{ type: 'text', text: 'hi' }])])), []);
  });
});

describe('withReasoningReplay', () => {
  it('sets custom_inputs.reasoning_replay from the header', () => {
    const body = withReasoningReplay('{"input":[]}', encodeHeaderJson([entry('toolu_1')]));

    assert.deepEqual(JSON.parse(body).custom_inputs, { reasoning_replay: [entry('toolu_1')] });
  });

  it('sends an empty list as an opt-in', () => {
    const body = withReasoningReplay('{"input":[]}', encodeHeaderJson([]));

    assert.deepEqual(JSON.parse(body).custom_inputs, { reasoning_replay: [] });
  });
});
