import { strict as assert } from 'node:assert';
import { describe, it } from 'node:test';
import type { ChatMessage } from '@chat-template/core';
import { sanitizeToolPairs } from '../../overrides/server/src/lib/sanitize-tool-pairs.ts';

// Tests build messages as plain objects and cast through `unknown`. This
// keeps the suite runnable without resolving the upstream
// `@chat-template/core` package at test time -- the import above is
// type-only and erased at runtime by Node's native TS stripping
// (`--experimental-strip-types`).

interface TextPart {
  type: 'text';
  text: string;
}

interface ToolPart {
  type: `tool-${string}`;
  toolCallId: string;
  state:
    | 'input-streaming'
    | 'input-available'
    | 'output-available'
    | 'output-error';
  input?: unknown;
  output?: unknown;
}

interface DynamicToolPart {
  type: 'dynamic-tool';
  toolName: string;
  toolCallId: string;
  state: ToolPart['state'];
  input?: unknown;
  output?: unknown;
}

type Part = TextPart | ToolPart | DynamicToolPart;

function makeMessage(
  id: string,
  role: 'user' | 'assistant',
  parts: Part[],
): ChatMessage {
  return { id, role, parts } as unknown as ChatMessage;
}

const text = (t: string): TextPart => ({ type: 'text', text: t });

const toolPart = (
  toolCallId: string,
  state: ToolPart['state'],
  name = 'calc',
): ToolPart => ({ type: `tool-${name}`, toolCallId, state, input: {} });

// AI SDK v5 emits this shape for MCP-backed / dynamically-registered
// tools.  Agent Bricks chat surfaces use this shape in production --
// see ``databricks-message-part-transformers.ts`` for the consumer
// side filtering ``p.type === 'dynamic-tool'`` parts.
const dynamicToolPart = (
  toolCallId: string,
  state: DynamicToolPart['state'],
  toolName = 'calc',
): DynamicToolPart => ({
  type: 'dynamic-tool',
  toolName,
  toolCallId,
  state,
  input: {},
});

describe('sanitizeToolPairs', () => {
  it('passes through an empty list', () => {
    const result = sanitizeToolPairs([]);
    assert.deepEqual(result.messages, []);
    assert.deepEqual(result.droppedDuplicates, []);
  });

  it('passes through a text-only conversation untouched', () => {
    const messages = [
      makeMessage('u1', 'user', [text('hi')]),
      makeMessage('a1', 'assistant', [text('hello')]),
    ];
    const result = sanitizeToolPairs(messages);
    assert.deepEqual(result.messages, messages);
    assert.equal(result.droppedDuplicates.length, 0);
  });

  it('keeps a balanced single tool turn (one part, output-available)', () => {
    // In AI SDK v5 a complete tool invocation lives on one part with
    // state output-available. convertToModelMessages later splits it
    // into a wire-level tool_use + tool_result pair on its own.
    const messages = [
      makeMessage('u1', 'user', [text("what's 2+2?")]),
      makeMessage('a1', 'assistant', [toolPart('c1', 'output-available')]),
      makeMessage('u2', 'user', [text('thanks')]),
    ];
    const result = sanitizeToolPairs(messages);
    assert.deepEqual(result.messages, messages);
    assert.equal(result.droppedDuplicates.length, 0);
  });

  it('does NOT drop a lone unresolved tool part (SDK does that)', () => {
    // ignoreIncompleteToolCalls in convertToModelMessages strips
    // input-streaming / input-available parts; this helper only
    // de-duplicates ids and must leave unique unresolved parts alone.
    const messages = [
      makeMessage('u1', 'user', [text('q')]),
      makeMessage('a1', 'assistant', [toolPart('c1', 'input-available')]),
    ];
    const result = sanitizeToolPairs(messages);
    assert.deepEqual(result.messages, messages);
    assert.equal(result.droppedDuplicates.length, 0);
  });

  it('drops the duplicate when the same id appears on two parts', () => {
    // Issue #297: same toolCallId on an unresolved part AND a resolved
    // part. convertToModelMessages would emit two tool_use blocks with
    // the same id; Anthropic rejects this with "unexpected tool_use_id
    // found in tool_result blocks". We keep the resolved one.
    const messages = [
      makeMessage('u1', 'user', [text('q')]),
      makeMessage('a1', 'assistant', [toolPart('dup', 'input-available')]),
      makeMessage('a2', 'assistant', [toolPart('dup', 'output-available')]),
    ];
    const result = sanitizeToolPairs(messages);

    assert.deepEqual(result.droppedDuplicates, ['dup']);
    assert.equal(result.messages.length, 2); // a1 dropped (parts emptied)
    assert.deepEqual(
      result.messages.map((m) => m.id),
      ['u1', 'a2'],
    );
  });

  it('prefers output-available over output-error on duplicate ids', () => {
    const messages = [
      makeMessage('u1', 'user', [text('q')]),
      makeMessage('a1', 'assistant', [toolPart('dup', 'output-error')]),
      makeMessage('a2', 'assistant', [toolPart('dup', 'output-available')]),
    ];
    const result = sanitizeToolPairs(messages);

    assert.deepEqual(result.droppedDuplicates, ['dup']);
    assert.equal(result.messages.length, 2);
    assert.deepEqual(
      result.messages.map((m) => m.id),
      ['u1', 'a2'],
    );
  });

  it('keeps the first occurrence on a tie (deterministic)', () => {
    // Two parts at the same rank -> first occurrence wins so the
    // sanitiser is stable across calls.
    const messages = [
      makeMessage('u1', 'user', [text('q')]),
      makeMessage('a1', 'assistant', [toolPart('dup', 'output-available')]),
      makeMessage('a2', 'assistant', [toolPart('dup', 'output-available')]),
    ];
    const result = sanitizeToolPairs(messages);

    assert.deepEqual(result.droppedDuplicates, ['dup']);
    assert.equal(result.messages.length, 2);
    assert.deepEqual(
      result.messages.map((m) => m.id),
      ['u1', 'a1'],
    );
  });

  it('preserves non-duplicate tool parts on the same message as a duplicate', () => {
    const messages = [
      makeMessage('u1', 'user', [text('q')]),
      makeMessage('a1', 'assistant', [
        toolPart('keep', 'output-available', 'genie'),
        toolPart('dup', 'input-available', 'docs'),
      ]),
      makeMessage('a2', 'assistant', [toolPart('dup', 'output-available', 'docs')]),
    ];
    const result = sanitizeToolPairs(messages);

    assert.deepEqual(result.droppedDuplicates, ['dup']);
    assert.equal(result.messages.length, 3);
    assert.equal((result.messages[1].parts as ToolPart[]).length, 1);
    assert.equal(
      (result.messages[1].parts[0] as ToolPart).toolCallId,
      'keep',
    );
  });

  it('leaves text parts on a partially-pruned message untouched', () => {
    const messages = [
      makeMessage('u1', 'user', [text('q')]),
      makeMessage('a1', 'assistant', [
        text('let me check'),
        toolPart('dup', 'input-available'),
        text('done'),
      ]),
      makeMessage('a2', 'assistant', [toolPart('dup', 'output-available')]),
    ];
    const result = sanitizeToolPairs(messages);

    assert.deepEqual(result.droppedDuplicates, ['dup']);
    assert.deepEqual(result.messages[1].parts, [
      text('let me check'),
      text('done'),
    ]);
  });

  it('handles multiple distinct duplicate ids in one pass', () => {
    const messages = [
      makeMessage('u1', 'user', [text('q')]),
      makeMessage('a1', 'assistant', [
        toolPart('a', 'input-available', 't1'),
        toolPart('b', 'input-available', 't2'),
      ]),
      makeMessage('a2', 'assistant', [
        toolPart('a', 'output-available', 't1'),
        toolPart('b', 'output-available', 't2'),
      ]),
    ];
    const result = sanitizeToolPairs(messages);

    assert.deepEqual([...result.droppedDuplicates].sort(), ['a', 'b']);
    assert.equal(result.messages.length, 2);
    assert.deepEqual(
      result.messages.map((m) => m.id),
      ['u1', 'a2'],
    );
  });

  // ``dynamic-tool`` parts.  Until this regression test set was added
  // the sanitiser silently no-op'd on Agent Bricks chat sessions
  // because ``isToolPart`` only recognised ``tool-<name>`` parts (the
  // statically-typed shape).  See the type comment in
  // ``sanitize-tool-pairs.ts`` for the AI SDK v5 dual-shape contract.

  it('keeps a balanced single dynamic-tool turn (output-available)', () => {
    const messages = [
      makeMessage('u1', 'user', [text("what's 2+2?")]),
      makeMessage('a1', 'assistant', [dynamicToolPart('c1', 'output-available')]),
      makeMessage('u2', 'user', [text('thanks')]),
    ];
    const result = sanitizeToolPairs(messages);
    assert.deepEqual(result.messages, messages);
    assert.equal(result.droppedDuplicates.length, 0);
  });

  it('drops the duplicate when the same id appears on two dynamic-tool parts', () => {
    const messages = [
      makeMessage('u1', 'user', [text('q')]),
      makeMessage('a1', 'assistant', [dynamicToolPart('dup', 'input-available')]),
      makeMessage('a2', 'assistant', [dynamicToolPart('dup', 'output-available')]),
    ];
    const result = sanitizeToolPairs(messages);

    assert.deepEqual(result.droppedDuplicates, ['dup']);
    assert.equal(result.messages.length, 2);
    assert.deepEqual(
      result.messages.map((m) => m.id),
      ['u1', 'a2'],
    );
  });

  it('dedupes across mixed tool-<name> and dynamic-tool parts sharing an id', () => {
    // Defensive: a session that crosses the static -> dynamic
    // registration boundary mid-conversation (e.g. an MCP tool list
    // refresh) could in principle re-emit the same ``toolCallId``
    // under the alternate shape.  The sanitiser must recognise both
    // shapes as the same pairing universe and dedupe across them.
    const messages = [
      makeMessage('u1', 'user', [text('q')]),
      makeMessage('a1', 'assistant', [toolPart('dup', 'input-available', 'genie')]),
      makeMessage('a2', 'assistant', [
        dynamicToolPart('dup', 'output-available', 'genie'),
      ]),
    ];
    const result = sanitizeToolPairs(messages);

    assert.deepEqual(result.droppedDuplicates, ['dup']);
    assert.equal(result.messages.length, 2);
    assert.deepEqual(
      result.messages.map((m) => m.id),
      ['u1', 'a2'],
    );
  });
});
