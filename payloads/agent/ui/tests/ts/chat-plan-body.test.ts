import { strict as assert } from 'node:assert';
import { describe, it } from 'node:test';
import { parsePlanBody } from '../../overrides/server/src/lib/chat-plan-body.ts';

const PLAN = '**Goal:** Ship the report\n1. [ ] Draft';

describe('parsePlanBody', () => {
  it('accepts a plan and its history', () => {
    assert.deepEqual(parsePlanBody({ content: PLAN, history: '' }, 100), { content: PLAN, history: '' });
  });

  it('accepts an empty plan, which is how the agent deletes one', () => {
    assert.deepEqual(parsePlanBody({ content: '', history: PLAN }, 100), { content: '', history: PLAN });
  });

  it('drops fields it does not store', () => {
    assert.deepEqual(parsePlanBody({ content: PLAN, history: '', chat_id: 'other' }, 100), {
      content: PLAN,
      history: '',
    });
  });

  for (const body of [null, 'plan', [], { content: PLAN }, { history: '' }, { content: 1, history: '' }]) {
    it(`rejects ${JSON.stringify(body)}`, () => {
      assert.equal(parsePlanBody(body, 100), null);
    });
  }

  it('rejects a plan or a history over the limit', () => {
    assert.equal(parsePlanBody({ content: 'x'.repeat(11), history: '' }, 10), null);
    assert.equal(parsePlanBody({ content: '', history: 'x'.repeat(11) }, 10), null);
    assert.deepEqual(parsePlanBody({ content: 'x'.repeat(10), history: 'y'.repeat(10) }, 10), {
      content: 'x'.repeat(10),
      history: 'y'.repeat(10),
    });
  });
});
