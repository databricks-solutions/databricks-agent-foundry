import { strict as assert } from 'node:assert';
import { describe, it } from 'node:test';
import type { ChatMessage } from '@chat-template/core';
import { CONTENT_FILTER_NOTICE, withoutRefusedTurns } from '../../overrides/server/src/lib/refused-turns.ts';

// Messages are plain objects cast through `unknown`; the `@chat-template/core`
// import is type-only and erased by Node's TS stripping.

type Part = Record<string, unknown> & { type: string };

function message(id: string, role: 'user' | 'assistant', parts: Part[]): ChatMessage {
  return { id, role, parts } as unknown as ChatMessage;
}

const text = (value: string): Part => ({ type: 'text', text: value });
const refusal = (id: string) => message(id, 'assistant', [{ type: 'step-start' }, text(CONTENT_FILTER_NOTICE)]);
const ids = (messages: ChatMessage[]) => messages.map((m) => m.id);

describe('withoutRefusedTurns', () => {
  it('drops each refusal and the user message it answered', () => {
    const chat = [
      message('u1', 'user', [text('Summarize the report')]),
      message('a1', 'assistant', [text('Here it is.')]),
      message('u2', 'user', [text('a refused question')]),
      refusal('a2'),
      message('u3', 'user', [text('another refused one')]),
      refusal('a3'),
      message('u4', 'user', [text('Tell me a joke')]),
    ];
    assert.deepEqual(ids(withoutRefusedTurns(chat)), ['u1', 'a1', 'u4']);
  });

  it('keeps the turn the compaction summary is attached to', () => {
    const chat = [message('u1', 'user', [text('a refused question')]), refusal('a1'), message('u2', 'user', [text('next')])];
    assert.deepEqual(ids(withoutRefusedTurns(chat, 'u1')), ['u1', 'a1', 'u2']);
  });

  it('keeps answers that only mention the notice or also called a tool', () => {
    const chat = [
      message('u1', 'user', [text('q')]),
      message('a1', 'assistant', [text(`Earlier I said: ${CONTENT_FILTER_NOTICE}`)]),
      message('u2', 'user', [text('q')]),
      message('a2', 'assistant', [
        { type: 'tool-execute_sql', toolCallId: 'c1', state: 'output-available', input: {}, output: 'ok' },
        text(CONTENT_FILTER_NOTICE),
      ]),
    ];
    assert.equal(withoutRefusedTurns(chat), chat);
  });

  it('returns the same list when nothing was refused', () => {
    const chat = [message('u1', 'user', [text('q')]), message('a1', 'assistant', [text('a')])];
    assert.equal(withoutRefusedTurns(chat), chat);
  });
});
