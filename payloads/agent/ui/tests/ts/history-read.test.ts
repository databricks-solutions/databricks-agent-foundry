import { strict as assert } from 'node:assert';
import { describe, it } from 'node:test';
import type { ChatMessage } from '@chat-template/core';
import { TOOL_LABEL_INPUT_CHARS, summarizedTurns } from '../../overrides/server/src/lib/compaction.ts';
import {
  MAX_OUTLINE_TOOL_CALLS,
  PAGE_CHARS,
  REQUEST_PREVIEW_CHARS,
  outlineHistory,
  readHistory,
} from '../../overrides/server/src/lib/history-read.ts';
import { searchHistory } from '../../overrides/server/src/lib/history-search.ts';

// Messages are plain objects cast through `unknown`; the `@chat-template/core`
// import is type-only and erased by Node's TS stripping.

type Part = Record<string, unknown> & { type: string };

function message(id: string, role: 'user' | 'assistant', parts: Part[]): ChatMessage {
  return { id, role, parts } as unknown as ChatMessage;
}

const text = (value: string): Part => ({ type: 'text', text: value });

function sql(query: string, output: string): Part {
  return { type: 'tool-execute_sql', state: 'output-available', input: { query }, output };
}

function summarize(messages: ChatMessage[]) {
  return summarizedTurns([...messages, message('next', 'user', [text('next')])], {
    boundaryMessageId: 'next',
    summary: 's',
  });
}

const ROWS = Array.from({ length: 2_000 }, (_, i) => `row ${i}: table_${i}`).join('\n');
const TURNS = summarize([
  message('u1', 'user', [text(`Give me a workspace overview. ${'Please be thorough. '.repeat(20)}`)]),
  message('a1', 'assistant', [text('Listing tables.'), sql('SHOW TABLES', ROWS)]),
  message('u2', 'user', [text('Thanks, and the owners?')]),
  message('a2', 'assistant', [text('All tables are owned by the service principal.')]),
]);

describe('outlineHistory', () => {
  it('lists each summarized turn with its request, tool calls and length', () => {
    const [first, second] = outlineHistory(TURNS);
    const tool = TURNS[0].passages[2];
    assert.deepEqual(first.toolCalls, [
      { label: 'execute_sql {"query":"SHOW TABLES"}', at: tool.start, chars: tool.end - tool.start },
    ]);
    assert.ok(TURNS[0].text.slice(first.toolCalls[0].at).startsWith('Assistant: [Tool execute_sql'));
    assert.deepEqual([first.chars, first.moreToolCalls], [TURNS[0].text.length, 0]);
    assert.deepEqual([second.request, second.toolCalls], ['User: Thanks, and the owners?', []]);
  });

  it('shortens long requests and tool inputs', () => {
    const [turn] = summarize([message('a0', 'assistant', [sql(`SELECT ${'x, '.repeat(100)}y`, 'ok')])]);
    const [outline] = outlineHistory([turn]);
    assert.equal(outline.toolCalls[0].label.length, 'execute_sql '.length + TOOL_LABEL_INPUT_CHARS + 1);
    const [first] = outlineHistory(TURNS);
    assert.ok(first.request.startsWith('User: Give me a workspace overview.'));
    assert.equal(first.request.length, REQUEST_PREVIEW_CHARS + 1);
    assert.ok(first.request.endsWith('…'));
  });

  it(`lists at most ${MAX_OUTLINE_TOOL_CALLS} tool calls and counts the rest`, () => {
    const calls = (n: number) => Array.from({ length: n }, (_, i) => sql(`SELECT ${i}`, 'ok'));
    const turns = summarize([
      message('u1', 'user', [text('first')]),
      message('a1', 'assistant', calls(MAX_OUTLINE_TOOL_CALLS - 2)),
      message('u2', 'user', [text('second')]),
      message('a2', 'assistant', calls(5)),
    ]);
    const outline = outlineHistory(turns);
    assert.deepEqual(
      outline.map((turn) => [turn.toolCalls.length, turn.moreToolCalls]),
      [
        [MAX_OUTLINE_TOOL_CALLS - 2, 0],
        [2, 3],
      ],
    );
  });

  it('is empty when nothing is summarized', () => {
    assert.deepEqual(outlineHistory([]), []);
  });
});

describe('readHistory', () => {
  it('pages through a turn, ending each page at a line break', () => {
    const first = readHistory(TURNS, 1, 0);
    assert.ok(first);
    assert.deepEqual([first.offset, first.turns, first.chars], [0, 2, TURNS[0].text.length]);
    assert.ok(first.end > PAGE_CHARS / 2 && first.end <= PAGE_CHARS);
    assert.ok(first.text.endsWith('\n'));
    const next = readHistory(TURNS, 1, first.end);
    assert.ok(next);
    assert.equal(next.text, TURNS[0].text.slice(first.end, next.end));
    assert.match(next.text, /^row \d+: table_\d+\n/);
  });

  it('ends a page after the last part that fits', () => {
    const block = 'y'.repeat(2_500);
    const [turn] = summarize([message('a0', 'assistant', [sql('A', block), sql('B', block), sql('C', block), sql('D', block)])]);
    const page = readHistory([turn], 1, 0);
    assert.ok(page);
    assert.equal(page.end, turn.passages[2].end + 1);
    assert.ok(turn.text.slice(page.end).startsWith('Assistant: [Tool execute_sql({"query":"D"})'));
  });

  it('cuts at PAGE_CHARS when the page has no break to end at', () => {
    const [turn] = summarize([message('a0', 'assistant', [text('z'.repeat(3 * PAGE_CHARS))])]);
    assert.equal(readHistory([turn], 1, 100)?.end, 100 + PAGE_CHARS);
  });

  it('opens the page a search hit points at', () => {
    const [hit] = searchHistory(TURNS, 'table_1999');
    const page = readHistory(TURNS, hit.turn, hit.at);
    assert.ok(page?.text.startsWith('table_1999'));
  });

  it('clamps the offset to the turn and returns the short last page', () => {
    const last = readHistory(TURNS, 2, 10_000);
    assert.ok(last);
    assert.deepEqual([last.offset, last.end, last.text], [last.chars, last.chars, '']);
    assert.equal(readHistory(TURNS, 2, 0)?.text, TURNS[1].text);
  });

  it('is null for a turn the summary did not replace', () => {
    assert.equal(readHistory(TURNS, 3, 0), null);
  });
});
