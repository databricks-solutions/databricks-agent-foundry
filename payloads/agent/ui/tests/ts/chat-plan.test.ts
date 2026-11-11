import { strict as assert } from 'node:assert';
import { describe, it } from 'node:test';
import type { ChatMessage } from '@chat-template/core';
import {
  type ChatPlan,
  finishedPlanCalls,
  parsePlan,
  planProgress,
} from '../../overrides/client/src/lib/chat-plan.ts';

// Type-only imports are erased by Node's TS stripping.

// As payloads/agent/runtime/plan.py renders it.
const STORED = [
  '**Goal:** Ship the report',
  '1. [x] Load the data — 1,204 rows',
  '2. [ ] Build the charts',
  '3. [-] Email the team — not needed',
].join('\n');

function message(parts: Array<Record<string, unknown>>): ChatMessage {
  return { id: 'm', role: 'assistant', parts } as unknown as ChatMessage;
}

function toolPart(toolName: string, state = 'output-available') {
  return { type: 'dynamic-tool', toolName, toolCallId: toolName, state };
}

describe('parsePlan', () => {
  it('reads the goal, statuses and notes', () => {
    assert.deepEqual(parsePlan(STORED), {
      goal: 'Ship the report',
      milestones: [
        { title: 'Load the data', status: 'done', note: '1,204 rows' },
        { title: 'Build the charts', status: 'open', note: '' },
        { title: 'Email the team', status: 'dropped', note: 'not needed' },
      ],
    });
  });

  it('reads older step checklists', () => {
    const plan = parsePlan('**Goal:** g\n- [>] Step one\n- [ ] Step two');
    assert.deepEqual(plan?.milestones.map((m) => m.status), ['open', 'open']);
  });

  it('splits the note at the first separator only', () => {
    assert.equal(parsePlan('**Goal:** g\n1. [x] a — b — c')?.milestones[0].note, 'b — c');
  });

  it('returns null without milestones', () => {
    assert.equal(parsePlan('**Goal:** g'), null);
    assert.equal(parsePlan(''), null);
  });
});

describe('planProgress', () => {
  it('leaves dropped milestones out', () => {
    assert.equal(planProgress(parsePlan(STORED) as ChatPlan), '1 of 2 done');
  });
});

describe('finishedPlanCalls', () => {
  it('counts finished plan updates and deletes only', () => {
    const messages = [
      message([toolPart('_update_plan'), { type: 'text', text: 'hi' }]),
      message([
        toolPart('_update_plan', 'input-available'),
        toolPart('_delete_plan'),
        toolPart('_remember'),
      ]),
    ];
    assert.equal(finishedPlanCalls(messages), 2);
  });
});
