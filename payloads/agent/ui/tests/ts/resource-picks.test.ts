import { strict as assert } from 'node:assert';
import { describe, it } from 'node:test';
import {
  buildPanel,
  canAdd,
  MAX_SELECTIONS,
  ofKind,
  withoutPick,
  withPick,
  type Pick,
  type ResourceEntry,
  type ResourcesResponse,
  type UserCatalogLimits,
  type UserSkill,
} from '../../overrides/client/src/lib/resource-picks.ts';

function entry(
  fqn: string,
  overrides: Partial<ResourceEntry> = {},
): ResourceEntry {
  return {
    kind: 'mcp_service',
    fqn,
    display_name: fqn.split('.').at(-1) ?? fqn,
    description: '',
    source: 'unity_gateway',
    identity: 'obo',
    origin: 'user_catalog',
    reason: null,
    ...overrides,
  };
}

const LIMITS: UserCatalogLimits = { max_servers: 2, max_tools: 60, max_skills: null };

function response(agent: ResourceEntry[], user: ResourceEntry[]): ResourcesResponse {
  return { agent, user, limits: LIMITS, user_reason: null, authoring: null };
}

const mcp = (fqn: string): Pick => ({ kind: 'mcp_service', fqn });

describe('buildPanel', () => {
  it('marks picks selected, included or unavailable', () => {
    const resources = response(
      [entry('team.mcp.jira', { identity: 'sp', origin: 'pinned' })],
      [entry('team.user.gmail'), entry('team.user.kafka')],
    );
    const picks = [mcp('team.user.gmail'), mcp('team.mcp.jira'), mcp('team.user.gone')];

    const model = buildPanel(resources, picks);

    assert.deepEqual(
      model.picked.map((row) => [row.pick.fqn, row.status]),
      [
        ['team.user.gmail', 'selected'],
        ['team.mcp.jira', 'included'],
        ['team.user.gone', 'unavailable'],
      ],
    );
    assert.equal(model.picked[2].entry, null);
  });

  it('offers only catalog entries not yet picked', () => {
    const resources = response([], [entry('team.user.gmail'), entry('team.user.kafka')]);

    const model = buildPanel(resources, [mcp('team.user.gmail')]);

    assert.deepEqual(model.available.map((e) => e.fqn), ['team.user.kafka']);
  });

  it('keeps kinds apart for the same FQN', () => {
    const resources = response([], [entry('team.x.same', { kind: 'skill' })]);

    const model = buildPanel(resources, [mcp('team.x.same')]);

    assert.equal(model.picked[0].status, 'unavailable');
    assert.equal(model.available.length, 1);
  });
});

describe('buildPanel with Lakebase user skills', () => {
  const weekly: UserSkill = {
    fqn: 'lakebase:weekly-report',
    display_name: 'weekly-report',
    description: 'Use for the weekly report.',
    body: 'Steps.',
  };
  const standup: UserSkill = { ...weekly, fqn: 'lakebase:standup', display_name: 'standup' };
  const skill = (fqn: string): Pick => ({ kind: 'skill', fqn });

  it('lists them with the catalog: picked ones selected, the rest available', () => {
    const model = buildPanel(response([], []), [skill('lakebase:weekly-report')], [weekly, standup]);

    assert.equal(model.picked[0].status, 'selected');
    assert.equal(model.picked[0].entry?.source, 'lakebase');
    assert.deepEqual(model.available.map((e) => e.fqn), ['lakebase:standup']);
  });

  it('shows a picked skill that was deleted as unavailable', () => {
    const model = buildPanel(response([], []), [skill('lakebase:gone')], [weekly]);

    assert.equal(model.picked[0].status, 'unavailable');
  });
});

describe('canAdd', () => {
  it('allows up to the kind limit', () => {
    assert.equal(canAdd('mcp_service', [mcp('a.b.c')], LIMITS), true);
    assert.equal(canAdd('mcp_service', [mcp('a.b.c'), mcp('a.b.d')], LIMITS), false);
  });

  it('refuses a kind the agent does not let users pick', () => {
    assert.equal(canAdd('skill', [], LIMITS), false);
  });

  it('counts only picks of the same kind', () => {
    const limits = { ...LIMITS, max_skills: 1 };
    const picks = [mcp('a.b.c'), mcp('a.b.d')];

    assert.equal(canAdd('skill', picks, limits), true);
  });

  it('stops at the hard cap', () => {
    const limits = { ...LIMITS, max_servers: 1000 };
    const picks = Array.from({ length: MAX_SELECTIONS }, (_, i) => mcp(`a.b.s${i}`));

    assert.equal(canAdd('mcp_service', picks, limits), false);
  });
});

describe('withPick / withoutPick', () => {
  it('adds once, keeping only kind and fqn', () => {
    const once = withPick([], entry('team.user.gmail'));
    const twice = withPick(once, entry('team.user.gmail'));

    assert.deepEqual(twice, [mcp('team.user.gmail')]);
  });

  it('removes by kind and fqn', () => {
    const picks = [mcp('a.b.c'), { kind: 'skill' as const, fqn: 'a.b.c' }];

    assert.deepEqual(withoutPick(picks, mcp('a.b.c')), [{ kind: 'skill', fqn: 'a.b.c' }]);
  });
});

describe('ofKind', () => {
  it('keeps one kind, in order, so catalogs never mix MCP and skills', () => {
    const skill = entry('team.skills.triage', { kind: 'skill' });
    const entries = [entry('team.user.gmail'), skill, entry('team.user.jira')];

    assert.deepEqual(ofKind(entries, 'skill'), [skill]);
    assert.deepEqual(
      ofKind(entries, 'mcp_service').map((e) => e.fqn),
      ['team.user.gmail', 'team.user.jira'],
    );
  });
});
