import { strict as assert } from 'node:assert';
import { describe, it } from 'node:test';
import {
  MAX_QUERY_CHARS,
  MAX_RECALL_HITS,
  factKey,
  isTopic,
  parseFactBody,
  parseMemoryQuery,
  searchFacts,
  topicChars,
} from '../../overrides/server/src/lib/agent-memory.ts';

describe('isTopic', () => {
  it('accepts the slugs the agent makes', () => {
    for (const topic of ['general', 'sql', 'team_setup', 'a', 'x'.repeat(64)]) {
      assert.equal(isTopic(topic), true, topic);
    }
  });

  it('rejects anything else', () => {
    for (const topic of ['', 'General', '-x', 'x_', 'a/b', '../etc', 'x'.repeat(65), 3, null]) {
      assert.equal(isTopic(topic), false, String(topic));
    }
  });
});

describe('parseFactBody', () => {
  it('collapses whitespace in the fact', () => {
    assert.deepEqual(parseFactBody({ topic: 'sql', fact: '  Prefers \n CTEs  ' }, 100), {
      topic: 'sql',
      fact: 'Prefers CTEs',
    });
  });

  it('rejects a bad topic, an empty fact and an overlong fact', () => {
    assert.ok('error' in parseFactBody({ topic: 'A B', fact: 'x' }, 100));
    assert.ok('error' in parseFactBody({ topic: 'sql', fact: '   ' }, 100));
    assert.ok('error' in parseFactBody({ topic: 'sql', fact: 'x'.repeat(11) }, 10));
    assert.ok('error' in parseFactBody(null, 100));
  });
});

describe('parseMemoryQuery', () => {
  it('keeps a query and rejects empty or overlong ones', () => {
    assert.equal(parseMemoryQuery({ query: 'warehouse' }), 'warehouse');
    assert.equal(parseMemoryQuery({ query: '  ' }), null);
    assert.equal(parseMemoryQuery({ query: 'x'.repeat(MAX_QUERY_CHARS + 1) }), null);
    assert.equal(parseMemoryQuery({}), null);
  });
});

describe('factKey and topicChars', () => {
  it('ignores case and spacing', () => {
    assert.equal(factKey('Prefers  CTEs'), factKey('prefers ctes'));
  });

  it('counts one newline between facts', () => {
    assert.equal(topicChars([]), 0);
    assert.equal(topicChars(['ab', 'cde']), 6);
  });
});

describe('searchFacts', () => {
  const facts = [
    { topic: 'sql', fact: 'Prefers CTEs over subqueries' },
    { topic: 'warehouse', fact: 'Uses the shared serverless warehouse' },
    { topic: 'team_setup', fact: 'Works in the payments team' },
  ];

  it('ranks the facts that match, best first', () => {
    assert.deepEqual(searchFacts(facts, 'serverless warehouse'), [facts[1]]);
  });

  it('matches on the topic name too', () => {
    assert.deepEqual(searchFacts(facts, 'team setup'), [facts[2]]);
  });

  it('finds nothing for no match or no terms', () => {
    assert.deepEqual(searchFacts(facts, 'kubernetes'), []);
    assert.deepEqual(searchFacts(facts, '?'), []);
  });

  it('returns at most the recall limit', () => {
    const many = Array.from({ length: MAX_RECALL_HITS + 5 }, (_, i) => ({ topic: 'sql', fact: `query ${i}` }));
    assert.equal(searchFacts(many, 'query').length, MAX_RECALL_HITS);
  });
});
