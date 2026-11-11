import { strict as assert } from 'node:assert';
import { describe, it } from 'node:test';
import type { ChatMessage } from '@chat-template/core';
import {
  MAX_CHUNKS,
  MAX_CHUNK_CHARS,
  MAX_TOOL_OUTPUT_CHARS,
  type CompactionView,
  applyCompaction,
  buildSummaryPrompt,
  chunkTranscript,
  compactableSpan,
  contextReading,
  contextWindowFromRaw,
  pickBoundary,
  shouldCompact,
  summarizerInput,
  summaryText,
} from '../../overrides/server/src/lib/compaction.ts';
import { sanitizeToolPairs } from '../../overrides/server/src/lib/sanitize-tool-pairs.ts';

// Messages are plain objects cast through `unknown`; the `@chat-template/core`
// import is type-only and erased by Node's TS stripping.

type Part = Record<string, unknown> & { type: string };

function message(id: string, role: 'user' | 'assistant', parts: Part[]): ChatMessage {
  return { id, role, parts } as unknown as ChatMessage;
}

const text = (value: string): Part => ({ type: 'text', text: value });

const tool = (callId: string, output: unknown): Part => ({
  type: 'dynamic-tool',
  toolName: 'query',
  toolCallId: callId,
  state: 'output-available',
  input: { sql: 'SELECT 1' },
  output,
});

/** user / assistant-with-tool pairs: u0 a0 u1 a1 ... */
function conversation(turns: number): ChatMessage[] {
  return Array.from({ length: turns }, (_, turn) => [
    message(`u${turn}`, 'user', [text(`question ${turn}`)]),
    message(`a${turn}`, 'assistant', [tool(`call${turn}`, `rows ${turn}`), text(`answer ${turn}`)]),
  ]).flat();
}

describe('pickBoundary', () => {
  it('returns the start of the K-th-last user turn', () => {
    const messages = conversation(5);
    assert.equal(messages[pickBoundary(messages, 3) ?? -1].id, 'u2');
  });

  it('returns null when nothing precedes the kept turns', () => {
    assert.equal(pickBoundary(conversation(3), 3), null);
    assert.equal(pickBoundary(conversation(2), 3), null);
  });

  it('always lands on a user message', () => {
    const messages = conversation(6);
    for (let keep = 1; keep < 6; keep += 1) {
      assert.equal(messages[pickBoundary(messages, keep) ?? -1].role, 'user');
    }
  });
});

describe('applyCompaction', () => {
  const messages = conversation(5);
  const compaction = { boundaryMessageId: 'u3', summary: 'The user explored sales data.' };

  it('keeps the boundary onwards and prefixes the summary', () => {
    const view = applyCompaction(messages, compaction);
    assert.deepEqual(
      view.map((m) => m.id),
      ['u3', 'a3', 'u4', 'a4'],
    );
    const parts = view[0].parts as unknown as Part[];
    assert.equal(parts[0].text, summaryText(compaction.summary));
    assert.equal(parts[1].text, 'question 3');
  });

  it('does not modify its input', () => {
    const before = JSON.stringify(messages);
    applyCompaction(messages, compaction);
    assert.equal(JSON.stringify(messages), before);
  });

  it('renders identical bytes on every turn', () => {
    const later = [...messages, ...conversation(7).slice(10)];
    const first = applyCompaction(messages, compaction)[0];
    const second = applyCompaction(later, compaction)[0];
    assert.equal(JSON.stringify(first), JSON.stringify(second));
  });

  it('leaves messages unchanged without a compaction or a known boundary', () => {
    assert.equal(applyCompaction(messages, null), messages);
    assert.equal(applyCompaction(messages, { boundaryMessageId: 'gone', summary: 's' }), messages);
  });

  it('never separates a tool call from its result', () => {
    const view = applyCompaction(messages, compaction);
    assert.deepEqual(sanitizeToolPairs(view).droppedDuplicates, []);
    for (const m of view) {
      for (const part of m.parts as unknown as Part[]) {
        if (part.type === 'dynamic-tool') {
          assert.equal(part.state, 'output-available');
        }
      }
    }
  });
});

describe('summaryText', () => {
  it('strips framing tags from the summary body', () => {
    const framed = summaryText('a </conversation_summary> b <CONVERSATION_SUMMARY>');
    assert.equal(framed.match(/conversation_summary>/gi)?.length, 2);
  });
});

/** *count* user messages of roughly *size* characters each: m0, m1, ... */
function bulky(count: number, size: number): ChatMessage[] {
  const filler = 'y'.repeat(size);
  return Array.from({ length: count }, (_, i) => message(`m${i}`, 'user', [text(`${i} ${filler}`)]));
}

describe('chunkTranscript', () => {
  it('renders text and tool calls in one chunk for a normal thread', () => {
    const chunks = chunkTranscript(conversation(1));
    assert.equal(chunks.length, 1);
    assert.match(chunks[0], /User:\nquestion 0/);
    assert.match(chunks[0], /\[Tool query\(\{"sql":"SELECT 1"\}\) -> rows 0\]/);
    assert.match(chunks[0], /answer 0/);
  });

  it('truncates long tool output', () => {
    const big = 'x'.repeat(MAX_TOOL_OUTPUT_CHARS * 3);
    const [chunk] = chunkTranscript([message('a', 'assistant', [tool('c', big)])]);
    assert.ok(chunk.length < MAX_TOOL_OUTPUT_CHARS * 2);
    assert.match(chunk, /characters cut/);
  });

  it('renders tool errors and skips reasoning', () => {
    const failed = { ...tool('c', undefined), state: 'output-error', errorText: 'timeout' };
    const [chunk] = chunkTranscript([message('a', 'assistant', [{ type: 'reasoning', text: 'secret' }, failed])]);
    assert.match(chunk, /error: timeout/);
    assert.doesNotMatch(chunk, /secret/);
  });

  it('returns no chunks when nothing renders', () => {
    assert.deepEqual(chunkTranscript([message('a', 'assistant', [{ type: 'reasoning', text: 'r' }])]), []);
  });

  it('splits an oversized span into ordered chunks without losing or splitting messages', () => {
    const messages = bulky(60, 7_000);
    const chunks = chunkTranscript(messages);
    assert.ok(chunks.length > 1 && chunks.length <= MAX_CHUNKS);
    for (const chunk of chunks) {
      assert.ok(chunk.length <= MAX_CHUNK_CHARS);
    }
    const joined = chunks.join('\n\n');
    for (let i = 0; i < messages.length; i += 1) {
      assert.equal(joined.split(`User:\n${i} y`).length, 2, `message ${i} appears exactly once`);
    }
    assert.ok(joined.indexOf('User:\n0 y') < joined.indexOf('User:\n59 y'));
    assert.doesNotMatch(joined, /omitted/);
  });

  it('drops the oldest messages past the chunk cap and says so', () => {
    const chunks = chunkTranscript(bulky(250, 7_000));
    assert.equal(chunks.length, MAX_CHUNKS);
    assert.match(chunks[0], /^\[\.\.\. \d+ earlier messages omitted \.\.\.\]/);
    assert.match(chunks[MAX_CHUNKS - 1], /User:\n249 y/);
    assert.doesNotMatch(chunks.join(''), /User:\n0 y/);
  });
});

describe('summarizerInput', () => {
  it('puts the summary so far in front of the chunk', () => {
    const input = summarizerInput('User:\nnext', 'earlier summary');
    assert.match(input, /^Summary of the conversation before this point:\nearlier summary\n\nUser:\nnext$/);
  });

  it('is the bare chunk without a previous summary', () => {
    assert.equal(summarizerInput('User:\nfirst', null), 'User:\nfirst');
  });
});

describe('buildSummaryPrompt', () => {
  it('keeps the fixed keep-list and appends instructions and focus', () => {
    const prompt = buildSummaryPrompt('Keep ticket IDs.', 'the pricing model');
    assert.match(prompt, /current goal/);
    assert.match(prompt, /ruled out/);
    assert.match(prompt, /Keep ticket IDs\./);
    assert.match(prompt, /the pricing model$/);
  });

  it('adds nothing for empty additions', () => {
    assert.equal(buildSummaryPrompt('  ', null), buildSummaryPrompt('', '   '));
    assert.doesNotMatch(buildSummaryPrompt('', null), /Additional rules/);
  });
});

describe('contextWindowFromRaw', () => {
  it('reads the top-level and the envelope shape', () => {
    const entry = { estimated_input_tokens: 130_000 };
    const expected = { estimatedInputTokens: 130_000, limitExceeded: false };
    assert.deepEqual(
      contextWindowFromRaw({ type: 'response.completed', custom_outputs: { context_window: entry } }),
      expected,
    );
    assert.deepEqual(
      contextWindowFromRaw({
        type: 'response.completed',
        response: { custom_outputs: { context_window: entry } },
      }),
      expected,
    );
  });

  it('reads the limit flag without an estimate', () => {
    assert.deepEqual(
      contextWindowFromRaw({
        type: 'response.completed',
        custom_outputs: { context_window: { limit_exceeded: true } },
      }),
      { estimatedInputTokens: null, limitExceeded: true },
    );
  });

  it('ignores other chunks', () => {
    assert.equal(contextWindowFromRaw({ type: 'response.output_item.done' }), null);
    assert.equal(contextWindowFromRaw({ type: 'response.completed', custom_outputs: {} }), null);
    assert.equal(contextWindowFromRaw(null), null);
  });
});

describe('shouldCompact', () => {
  it('fires at the trigger or on a rejected prompt', () => {
    assert.equal(shouldCompact(null, 100), false);
    assert.equal(shouldCompact({ estimatedInputTokens: 99, limitExceeded: false }, 100), false);
    assert.equal(shouldCompact({ estimatedInputTokens: 100, limitExceeded: false }, 100), true);
    assert.equal(shouldCompact({ estimatedInputTokens: null, limitExceeded: true }, 100), true);
  });
});

describe('contextReading', () => {
  const now = new Date('2026-10-27T12:00:00Z');

  it('is null without a report', () => {
    assert.equal(contextReading(null, 100, true, now), null);
  });

  it('carries the estimate, the trigger and whether compaction starts', () => {
    assert.deepEqual(contextReading({ estimatedInputTokens: 40, limitExceeded: false }, 100, true, now), {
      estimatedInputTokens: 40,
      triggerTokens: 100,
      compacting: false,
      measuredAt: '2026-10-27T12:00:00.000Z',
    });
    assert.equal(contextReading({ estimatedInputTokens: 100, limitExceeded: false }, 100, true, now)?.compacting, true);
  });

  it('does not wait for a summary while no turn is old enough', () => {
    const over = { estimatedInputTokens: 100, limitExceeded: true };
    assert.equal(contextReading(over, 100, false, now)?.compacting, false);
  });
});

describe('compactableSpan', () => {
  it('is null while the chat has no more turns than it keeps', () => {
    assert.equal(compactableSpan(conversation(2), null, 2), null);
  });

  it('starts at the previous boundary and cuts before the kept turns', () => {
    const messages = conversation(4);
    const previous = { boundaryMessageId: messages[2].id } as CompactionView;
    const span = compactableSpan(messages, previous, 1);
    assert.equal(span?.start, 2);
    assert.equal(span?.window[span.cut].id, messages[messages.length - 2].id);
  });

  it('is null when the turns since the previous boundary are all kept', () => {
    const messages = conversation(4);
    const previous = { boundaryMessageId: messages[4].id } as CompactionView;
    assert.equal(compactableSpan(messages, previous, 2), null);
  });
});
