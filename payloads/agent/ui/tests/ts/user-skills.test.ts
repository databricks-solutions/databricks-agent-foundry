import { strict as assert } from 'node:assert';
import { describe, it } from 'node:test';
import {
  acceptedSkillSaves,
  MAX_DESCRIPTION_CHARS,
  MAX_SLUG_CHARS,
  parseSkillDraft,
  parseSkillEdit,
  slugFromFqn,
  splitUserSkillPicks,
  toAuthoredSkill,
  toUserSkillView,
  userSkillFqn,
} from '../../overrides/server/src/lib/user-skills.ts';

const MAX_BODY = 100;
const draft = { slug: 'weekly-report', description: 'Use for the weekly report.', body: 'Steps.' };

describe('user skill ids', () => {
  it('round-trips a slug through the lakebase: prefix', () => {
    assert.equal(userSkillFqn('weekly-report'), 'lakebase:weekly-report');
    assert.equal(slugFromFqn('lakebase:weekly-report'), 'weekly-report');
  });

  it('rejects Unity Catalog names and malformed slugs', () => {
    for (const fqn of ['main.skills.weekly', 'lakebase:', 'lakebase:Bad', 'lakebase:a--b', 'lakebase:../x']) {
      assert.equal(slugFromFqn(fqn), undefined, fqn);
    }
  });
});

describe('parseSkillDraft', () => {
  it('accepts a valid draft', () => {
    assert.deepEqual(parseSkillDraft(draft, MAX_BODY), { ok: true, value: draft });
  });

  it('rejects unknown keys and non-objects', () => {
    assert.equal(parseSkillDraft({ ...draft, owner: 'someone' }, MAX_BODY).ok, false);
    assert.equal(parseSkillDraft([draft], MAX_BODY).ok, false);
    assert.equal(parseSkillDraft(null, MAX_BODY).ok, false);
  });

  it('enforces the slug, description and body rules', () => {
    const cases = [
      { ...draft, slug: 'Weekly' },
      { ...draft, slug: 'a'.repeat(MAX_SLUG_CHARS + 1) },
      { ...draft, description: '   ' },
      { ...draft, description: 'x'.repeat(MAX_DESCRIPTION_CHARS + 1) },
      { ...draft, body: '' },
      { ...draft, body: 'x'.repeat(MAX_BODY + 1) },
    ];
    for (const candidate of cases) {
      assert.equal(parseSkillDraft(candidate, MAX_BODY).ok, false, JSON.stringify(candidate).slice(0, 60));
    }
  });
});

describe('parseSkillEdit', () => {
  it('accepts description and body only', () => {
    const edit = { description: 'New.', body: 'New steps.' };
    assert.deepEqual(parseSkillEdit(edit, MAX_BODY), { ok: true, value: edit });
    assert.equal(parseSkillEdit({ ...edit, slug: 'rename' }, MAX_BODY).ok, false);
  });
});

describe('splitUserSkillPicks', () => {
  it('separates Lakebase skill slugs from Unity Catalog picks, keeping order', () => {
    const picks = [
      { kind: 'mcp_service', fqn: 'team.mcp.jira' },
      { kind: 'skill', fqn: 'lakebase:weekly-report' },
      { kind: 'skill', fqn: 'team.skills.triage' },
      { kind: 'skill', fqn: 'lakebase:standup' },
    ];
    assert.deepEqual(splitUserSkillPicks(picks), {
      others: [picks[0], picks[2]],
      slugs: ['weekly-report', 'standup'],
    });
  });

  it('drops invalid picks under the prefix instead of passing them on', () => {
    const picks = [
      { kind: 'mcp_service', fqn: 'lakebase:weekly-report' },
      { kind: 'skill', fqn: 'lakebase:Bad' },
    ];
    assert.deepEqual(splitUserSkillPicks(picks), { others: [], slugs: [] });
  });
});

describe('response shapes', () => {
  it('match the New skill form and the panel list', () => {
    assert.deepEqual(toAuthoredSkill(draft), {
      fqn: 'lakebase:weekly-report',
      display_name: 'weekly-report',
      description: draft.description,
    });
    assert.equal(toUserSkillView(draft).body, draft.body);
  });
});

describe('acceptedSkillSaves', () => {
  const call = (toolCallId: string, input: unknown, toolName = '_save_skill') => ({
    toolCallId,
    toolName,
    input,
  });

  it('returns the agent-accepted _save_skill inputs in order, parsing JSON strings', () => {
    const steps = [
      {
        toolCalls: [
          call('1', draft),
          call('2', JSON.stringify({ ...draft, slug: 'standup' })),
          call('3', { skill_id: 'lakebase:x' }, '_load_skill'),
        ],
        toolResults: [{ toolCallId: '1', output: 'Saving skill `lakebase:weekly-report`' }],
      },
    ];
    assert.deepEqual(acceptedSkillSaves(steps), [draft, { ...draft, slug: 'standup' }]);
  });

  it('skips calls the agent refused, so the user is not told twice', () => {
    const steps = [
      {
        toolCalls: [call('1', draft)],
        toolResults: [{ toolCallId: '1', output: 'Error: the skill was not saved (slug: ...)' }],
      },
    ];
    assert.deepEqual(acceptedSkillSaves(steps), []);
  });
});
