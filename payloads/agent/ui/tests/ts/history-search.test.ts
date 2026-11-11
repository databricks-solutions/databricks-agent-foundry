import { strict as assert } from 'node:assert';
import { describe, it } from 'node:test';
import type { ChatMessage } from '@chat-template/core';
import {
  MAX_SEARCH_PART_CHARS,
  type TranscriptTurn,
  summarizedTurns,
} from '../../overrides/server/src/lib/compaction.ts';
import { EXCERPT_CHARS, MAX_HITS, searchHistory } from '../../overrides/server/src/lib/history-search.ts';

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

const CHAT = [
  message('u1', 'user', [text('Which warehouse should I use?')]),
  message('a1', 'assistant', [text('Checking.'), sql('SHOW WAREHOUSES', 'warehouse id 0123456789abcdef is running')]),
  message('u2', 'user', [text('And the workspace id?')]),
  message('a2', 'assistant', [text('The workspace id is 1234567890123456.')]),
  message('u3', 'user', [text('Thanks')]),
];
const SUMMARIZED = summarizedTurns(CHAT, { boundaryMessageId: 'u3', summary: 's' });

function summarize(messages: ChatMessage[]): TranscriptTurn[] {
  return summarizedTurns([...messages, message('next', 'user', [text('next')])], {
    boundaryMessageId: 'next',
    summary: 's',
  });
}

describe('summarizedTurns', () => {
  it('renders the turns before the boundary, one span per part', () => {
    assert.deepEqual(
      SUMMARIZED.map((turn) => [turn.turn, turn.passages.map((span) => span.kind)]),
      [
        [1, ['user', 'assistant', 'tool']],
        [2, ['user', 'assistant']],
      ],
    );
    assert.ok(SUMMARIZED.every((turn) => !turn.text.includes('Thanks')));
    const [, , tool] = SUMMARIZED[0].passages;
    assert.match(SUMMARIZED[0].text.slice(tool.start, tool.end), /^Assistant: \[Tool execute_sql.*0123456789abcdef/s);
  });

  it('keeps whole tool outputs, unlike the summarizer input', () => {
    const rows = Array.from({ length: 200 }, (_, i) => `row ${i}: table_${i}`).join('\n');
    const [turn] = summarize([message('a0', 'assistant', [sql('SHOW TABLES', rows)])]);
    assert.match(turn.text, /row 199: table_199/);
    assert.match(searchHistory([turn], 'table_199')[0].excerpt, /table_199/);
  });

  it('renders MCP text-part results without escaping their JSON again', () => {
    const result = '{"columns":["catalog"],"rows":[["shared_catalog"]]}';
    const parts = [{ type: 'input_text', text: result }];
    for (const output of [parts, JSON.stringify(parts)]) {
      const [turn] = summarize([message('a0', 'assistant', [sql('SHOW CATALOGS', output as string)])]);
      assert.ok(turn.text.endsWith(`-> ${result}]`), turn.text);
    }
  });

  it('caps each part at MAX_SEARCH_PART_CHARS', () => {
    const [turn] = summarize([message('a0', 'assistant', [text(`${'x'.repeat(MAX_SEARCH_PART_CHARS)}needle`)])]);
    assert.ok(turn.text.length < MAX_SEARCH_PART_CHARS + 100);
    assert.deepEqual(searchHistory([turn], 'needle'), []);
  });

  it('is empty without a compaction or with an unknown boundary', () => {
    assert.deepEqual(summarizedTurns(CHAT, null), []);
    assert.deepEqual(summarizedTurns(CHAT, { boundaryMessageId: 'gone', summary: 's' }), []);
  });
});

describe('searchHistory', () => {
  it('finds tool outputs with their turn and offset in the turn', () => {
    const [hit] = searchHistory(SUMMARIZED, 'warehouse id');
    assert.equal(hit.turn, 1);
    assert.match(hit.excerpt, /0123456789abcdef/);
    assert.ok(SUMMARIZED[0].text.slice(hit.at).startsWith('warehouse id'));
  });

  it('ranks the whole phrase above scattered terms', () => {
    const [hit] = searchHistory(SUMMARIZED, 'workspace id');
    assert.equal(hit.turn, 2);
    assert.match(hit.excerpt, /1234567890123456/);
  });

  it('matches case-insensitively and ignores one-letter terms', () => {
    assert.equal(searchHistory(SUMMARIZED, 'WAREHOUSE').length, 2);
    assert.deepEqual(searchHistory(SUMMARIZED, 'a'), []);
    assert.deepEqual(searchHistory(SUMMARIZED, '   '), []);
  });

  it('returns nothing when no part matches', () => {
    assert.deepEqual(searchHistory(SUMMARIZED, 'lakebase'), []);
  });

  it('finds a rare term deep in one tool result of a long turn', () => {
    const catalogs = Array.from({ length: 300 }, (_, i) => `["catalog_${i}","owner_${i}@example.com"]`);
    catalogs.splice(250, 0, '["shared_catalog","jane.doe@example.com"]');
    const turns = summarize([
      message('u1', 'user', [text('Give me a workspace overview')]),
      message('a1', 'assistant', [
        text('Plan: catalogs inventoried, owners checked.'),
        sql('SELECT catalog_name, catalog_owner FROM system.information_schema.catalogs', `[${catalogs.join(',')}]`),
      ]),
    ]);
    const [hit] = searchHistory(turns, 'jane.doe@example.com catalog owner');
    assert.match(hit.excerpt, /shared_catalog","jane\.doe@example\.com/);
    assert.ok(turns[0].text.slice(hit.at).startsWith('jane.doe@example.com'));
  });

  it('caps hits and excerpt length, newest first on ties', () => {
    const long = `${'x'.repeat(2_000)} needle ${'y'.repeat(2_000)}`;
    const many: TranscriptTurn[] = Array.from({ length: MAX_HITS + 3 }, (_, i) => ({
      turn: i + 1,
      text: long,
      passages: [{ start: 0, end: long.length, kind: 'user' }],
    }));
    const hits = searchHistory(many, 'needle');
    assert.equal(hits.length, MAX_HITS);
    assert.ok(hits.every((hit) => hit.excerpt.includes('needle')));
    assert.ok(hits.every((hit) => hit.excerpt.length <= EXCERPT_CHARS + 2));
    assert.equal(hits[0].turn, MAX_HITS + 3);
  });
});
