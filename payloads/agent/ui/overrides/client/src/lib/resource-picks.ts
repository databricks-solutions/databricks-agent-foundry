/**
 * Agent Bricks: the resources panel model.
 *
 * Merges the agent's view (`GET /api/agent/resources`) with the user's
 * saved picks (`GET /api/preferences/resources`).  The agent re-checks
 * every pick on every request, so the limits here only steer the UI.
 *
 * Kept free of runtime imports so `payloads/agent/ui/tests/ts/` can test it.
 */

import type { AuthoringInfo } from './skill-draft.ts';

export type ResourceKind = 'mcp_service' | 'skill';
export type Identity = 'obo' | 'sp';

export interface ResourceEntry {
  kind: ResourceKind;
  fqn: string;
  display_name: string;
  description: string;
  source: 'unity_gateway' | 'workspace' | 'foundry' | 'lakebase';
  identity: Identity;
  origin: 'pinned' | 'discovered' | 'configured' | 'user_catalog';
  reason: string | null;
}

export interface UserCatalogLimits {
  max_servers: number | null;
  max_tools: number | null;
  max_skills: number | null;
}

export interface ResourcesResponse {
  agent: ResourceEntry[];
  user: ResourceEntry[];
  limits: UserCatalogLimits;
  user_reason: 'no_user_token' | 'listing_failed' | null;
  authoring: AuthoringInfo | null;
}

/** One of the user's Lakebase skills (`GET /api/user-skills`). */
export interface UserSkill {
  fqn: string;
  display_name: string;
  description: string;
  body: string;
}

export interface Pick {
  kind: ResourceKind;
  fqn: string;
}

/**
 * `selected`: in the user's catalog and sent with each message.
 * `included`: the agent already uses it, so the pick changes nothing.
 * `unavailable`: no longer in the user's catalog (removed, or access lost).
 */
export type PickStatus = 'selected' | 'included' | 'unavailable';

export interface PickRow {
  pick: Pick;
  entry: ResourceEntry | null;
  status: PickStatus;
}

export interface PanelModel {
  agent: ResourceEntry[];
  picked: PickRow[];
  available: ResourceEntry[];
}

export const MAX_SELECTIONS = 64;

export function pickKey(pick: Pick): string {
  return `${pick.kind}:${pick.fqn}`;
}

function pickRow(
  pick: Pick,
  catalog: Map<string, ResourceEntry>,
  agent: Map<string, ResourceEntry>,
): PickRow {
  const key = pickKey(pick);
  const entry = catalog.get(key);
  if (entry) {
    return { pick, entry, status: 'selected' };
  }
  const agentEntry = agent.get(key);
  if (agentEntry) {
    return { pick, entry: agentEntry, status: 'included' };
  }
  return { pick, entry: null, status: 'unavailable' };
}

function byKey(entries: ResourceEntry[]): Map<string, ResourceEntry> {
  return new Map(entries.map((entry) => [pickKey(entry), entry]));
}

function userSkillEntry(skill: UserSkill): ResourceEntry {
  return {
    kind: 'skill',
    fqn: skill.fqn,
    display_name: skill.display_name,
    description: skill.description,
    source: 'lakebase',
    identity: 'obo',
    origin: 'user_catalog',
    reason: null,
  };
}

/** The user's Lakebase skills count as part of their own catalog. */
export function buildPanel(
  resources: ResourcesResponse,
  picks: Pick[],
  userSkills: UserSkill[] = [],
): PanelModel {
  const user = [...resources.user, ...userSkills.map(userSkillEntry)];
  const catalog = byKey(user);
  const agent = byKey(resources.agent);
  const picked = new Set(picks.map(pickKey));
  return {
    agent: resources.agent,
    picked: picks.map((pick) => pickRow(pick, catalog, agent)),
    available: user.filter((entry) => !picked.has(pickKey(entry))),
  };
}

export function ofKind(
  entries: ResourceEntry[],
  kind: ResourceKind,
): ResourceEntry[] {
  return entries.filter((entry) => entry.kind === kind);
}

export function limitFor(
  kind: ResourceKind,
  limits: UserCatalogLimits,
): number | null {
  return kind === 'mcp_service' ? limits.max_servers : limits.max_skills;
}

/** Whether one more pick of *kind* stays within the agent's limits. */
export function canAdd(
  kind: ResourceKind,
  picks: Pick[],
  limits: UserCatalogLimits,
): boolean {
  const limit = limitFor(kind, limits);
  if (limit === null || picks.length >= MAX_SELECTIONS) {
    return false;
  }
  return picks.filter((pick) => pick.kind === kind).length < limit;
}

export function withPick(picks: Pick[], pick: Pick): Pick[] {
  const key = pickKey(pick);
  return picks.some((existing) => pickKey(existing) === key)
    ? picks
    : [...picks, { kind: pick.kind, fqn: pick.fqn }];
}

export function withoutPick(picks: Pick[], pick: Pick): Pick[] {
  const key = pickKey(pick);
  return picks.filter((existing) => pickKey(existing) !== key);
}
