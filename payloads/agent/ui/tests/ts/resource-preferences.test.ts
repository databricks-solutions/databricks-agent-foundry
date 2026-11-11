import { strict as assert } from 'node:assert';
import { describe, it } from 'node:test';
import {
  MAX_SELECTIONS,
  MemoryPreferenceStore,
  parseSelectedResources,
} from '../../overrides/server/src/lib/resource-preferences.ts';
import { isUserSkillFqn } from '../../overrides/server/src/lib/user-skills.ts';

const pick = (fqn: string, kind = 'mcp_service') => ({ kind, fqn });

describe('parseSelectedResources', () => {
  it('accepts well-formed picks of both kinds', () => {
    const result = parseSelectedResources([
      pick('team.mcp.jira'),
      pick('team.skills.triage', 'skill'),
    ]);
    assert.deepEqual(result, {
      ok: true,
      resources: [pick('team.mcp.jira'), pick('team.skills.triage', 'skill')],
    });
  });

  it('accepts the same FQN under different kinds', () => {
    const result = parseSelectedResources([
      pick('team.x.same'),
      pick('team.x.same', 'skill'),
    ]);
    assert.equal(result.ok, true);
  });

  it('rejects anything but an array', () => {
    assert.equal(parseSelectedResources({ kind: 'skill' }).ok, false);
    assert.equal(parseSelectedResources(undefined).ok, false);
  });

  it('rejects more than the hard cap', () => {
    const many = Array.from({ length: MAX_SELECTIONS + 1 }, (_, i) =>
      pick(`team.mcp.s${i}`),
    );
    assert.equal(parseSelectedResources(many).ok, false);
  });

  it('rejects duplicates', () => {
    const result = parseSelectedResources([
      pick('team.mcp.jira'),
      pick('team.mcp.jira'),
    ]);
    assert.deepEqual(result, {
      ok: false,
      error: 'resources[1] is a duplicate',
    });
  });

  for (const [name, item] of [
    ['unknown kind', pick('team.mcp.jira', 'model')],
    ['two-part name', pick('team.jira')],
    ['path characters', pick('team.mcp.../etc')],
    ['URL characters', pick('team.mcp.jira?x=1')],
    ['leading dash', pick('team.mcp.-jira')],
    ['over-long name', pick(`team.mcp.${'a'.repeat(250)}`)],
    ['extra keys', { ...pick('team.mcp.jira'), identity: 'sp' }],
    ['non-object', 'team.mcp.jira'],
  ] as const) {
    it(`rejects ${name}`, () => {
      assert.equal(parseSelectedResources([item]).ok, false);
    });
  }
});

describe('parseSelectedResources with user skills', () => {
  const userSkill = pick('lakebase:weekly-report', 'skill');

  it('accepts a user skill pick only when the check is passed', () => {
    assert.equal(parseSelectedResources([userSkill]).ok, false);
    assert.deepEqual(parseSelectedResources([userSkill], isUserSkillFqn), {
      ok: true,
      resources: [userSkill],
    });
  });

  it('keeps Unity Catalog picks valid alongside', () => {
    const result = parseSelectedResources(
      [pick('team.mcp.jira'), userSkill],
      isUserSkillFqn,
    );
    assert.equal(result.ok, true);
  });

  for (const [name, item] of [
    ['an MCP server', pick('lakebase:weekly-report')],
    ['a malformed slug', pick('lakebase:Weekly', 'skill')],
    ['an empty slug', pick('lakebase:', 'skill')],
  ] as const) {
    it(`rejects ${name} under the lakebase: prefix`, () => {
      assert.equal(parseSelectedResources([item], isUserSkillFqn).ok, false);
    });
  }
});

describe('MemoryPreferenceStore', () => {
  it('keeps picks per user and starts empty', async () => {
    const store = new MemoryPreferenceStore();
    await store.put('alice', [pick('team.mcp.jira')]);

    assert.deepEqual(await store.get('alice'), [pick('team.mcp.jira')]);
    assert.deepEqual(await store.get('bob'), []);
  });

  it('stores a copy, not the caller array', async () => {
    const store = new MemoryPreferenceStore();
    const picks = [pick('team.mcp.jira')];
    await store.put('alice', picks);
    picks.push(pick('team.mcp.kafka'));

    assert.equal((await store.get('alice')).length, 1);
  });
});
