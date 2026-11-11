import { strict as assert } from 'node:assert';
import { describe, it } from 'node:test';
import type {
  Pick,
  UserCatalogLimits,
} from '../../overrides/client/src/lib/resource-picks.ts';
import {
  autoSelectNewSkill,
  savedInText,
  skillSavedText,
  validateDraft,
  type AuthoredSkill,
  type AuthoringInfo,
  type SkillDraft,
} from '../../overrides/client/src/lib/skill-draft.ts';

const SKILL: AuthoredSkill = {
  fqn: 'team.user_skills.u-jane-doe-triage',
  display_name: 'u-jane-doe-triage',
  description: 'Sort tickets',
};
const LIMITS: UserCatalogLimits = { max_servers: 2, max_tools: null, max_skills: 2 };
const AUTHORING: AuthoringInfo = {
  schema_fqn: 'team.user_skills',
  max_skill_md_chars: 100,
  unavailable_reason: null,
  store: 'unity_catalog',
};
const DRAFT: SkillDraft = { slug: 'triage', description: 'Sort', body: 'Do it' };

function skillPick(fqn: string): Pick {
  return { kind: 'skill', fqn };
}

describe('autoSelectNewSkill', () => {
  it('adds the new skill to the picks', () => {
    const picks = [{ kind: 'mcp_service', fqn: 'c.s.m' } satisfies Pick];

    assert.deepEqual(autoSelectNewSkill(picks, SKILL, LIMITS, 'unity_catalog'), [
      ...picks,
      skillPick(SKILL.fqn),
    ]);
  });

  it('keeps the picks when the skill limit is reached', () => {
    const picks = [skillPick('c.s.a'), skillPick('c.s.b')];

    assert.equal(autoSelectNewSkill(picks, SKILL, LIMITS, 'unity_catalog'), picks);
  });

  it('keeps the picks when skills cannot be picked at all', () => {
    const limits = { ...LIMITS, max_skills: null };

    assert.deepEqual(autoSelectNewSkill([], SKILL, limits, 'unity_catalog'), []);
  });

  it('does not add the skill twice', () => {
    const picks = [skillPick(SKILL.fqn)];

    assert.deepEqual(autoSelectNewSkill(picks, SKILL, LIMITS, 'unity_catalog'), picks);
  });

  it('leaves a new Lakebase skill in the catalog', () => {
    const skill = { ...SKILL, fqn: 'lakebase:triage' };

    assert.deepEqual(autoSelectNewSkill([], skill, LIMITS, 'lakebase'), []);
  });
});

describe('validateDraft', () => {
  it('accepts a complete draft', () => {
    assert.deepEqual(validateDraft(DRAFT, AUTHORING), {});
  });

  for (const slug of ['Triage', 'a_b', '-a', 'a-', 'a--b', '../a', '', 'a'.repeat(41)]) {
    it(`rejects the slug ${JSON.stringify(slug)}`, () => {
      assert.ok(validateDraft({ ...DRAFT, slug }, AUTHORING).slug);
    });
  }

  it('needs a description within the cap', () => {
    assert.ok(validateDraft({ ...DRAFT, description: '  ' }, AUTHORING).description);
    assert.ok(
      validateDraft({ ...DRAFT, description: 'd'.repeat(1025) }, AUTHORING).description,
    );
  });

  it('needs a body within the SKILL.md cap', () => {
    assert.ok(validateDraft({ ...DRAFT, body: '\n' }, AUTHORING).body);
    assert.ok(validateDraft({ ...DRAFT, body: 'x'.repeat(101) }, AUTHORING).body);
  });
});

describe('savedInText', () => {
  it('names the Unity Catalog schema, or the account for Lakebase', () => {
    assert.equal(savedInText(AUTHORING), 'Saved in team.user_skills as you.');
    assert.equal(
      savedInText({ ...AUTHORING, schema_fqn: '', store: 'lakebase' }),
      'Saved to your account; only you can see it.',
    );
  });
});

describe('skillSavedText', () => {
  it('says what the agent saved, or why it could not', () => {
    assert.equal(
      skillSavedText({ ok: true, fqn: 'lakebase:triage', display_name: 'triage', created: true }),
      'Created skill triage; it is picked from your next message.',
    );
    assert.equal(
      skillSavedText({ ok: true, fqn: 'lakebase:triage', display_name: 'triage', created: false }),
      'Updated skill triage; it is picked from your next message.',
    );
    assert.equal(
      skillSavedText({ ok: false, slug: 'triage', error: 'You already have 20 skills, the maximum.' }),
      'The agent could not save the skill triage: You already have 20 skills, the maximum.',
    );
  });
});
