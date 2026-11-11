/**
 * Agent Bricks: the user's sidebar picks of Unity Gateway resources.
 *
 * Express only checks shape, duplicates and the hard cap; the agent
 * re-checks every pick against its `user_catalog` policy and limits on
 * every request (`payloads/agent/runtime/gateway_selection.py`), so these
 * rules mirror that parser and must stay at least as strict.
 *
 * Kept free of runtime imports so `payloads/agent/ui/tests/ts/` can test it.
 */

export const MAX_SELECTIONS = 64;
const MAX_FQN_CHARS = 255;
const UC_NAME_PART = '[A-Za-z0-9_][A-Za-z0-9_-]*';
const SECURABLE_FQN = new RegExp(
  `^${UC_NAME_PART}\\.${UC_NAME_PART}\\.${UC_NAME_PART}$`,
);
const RESOURCE_KINDS = ['mcp_service', 'skill'] as const;

export type ResourceKind = (typeof RESOURCE_KINDS)[number];

export interface SelectedResource {
  kind: ResourceKind;
  fqn: string;
}

/** Accepts skill names beyond Unity Catalog ones, e.g. `lakebase:<slug>`. */
export type SkillFqnCheck = (fqn: string) => boolean;

export type ParseResult =
  | { ok: true; resources: SelectedResource[] }
  | { ok: false; error: string };

export interface PreferenceStore {
  get(userId: string): Promise<SelectedResource[]>;
  put(userId: string, resources: SelectedResource[]): Promise<void>;
}

function isResourceKind(value: unknown): value is ResourceKind {
  return RESOURCE_KINDS.includes(value as ResourceKind);
}

function isPickFqn(
  kind: ResourceKind,
  fqn: string,
  acceptsUserSkill?: SkillFqnCheck,
): boolean {
  if (fqn.length > MAX_FQN_CHARS) {
    return false;
  }
  return (
    SECURABLE_FQN.test(fqn) ||
    (kind === 'skill' && acceptsUserSkill !== undefined && acceptsUserSkill(fqn))
  );
}

function parseResource(
  raw: unknown,
  acceptsUserSkill?: SkillFqnCheck,
): SelectedResource | null {
  if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) {
    return null;
  }
  const { kind, fqn, ...rest } = raw as Record<string, unknown>;
  if (Object.keys(rest).length > 0 || !isResourceKind(kind)) {
    return null;
  }
  if (typeof fqn !== 'string' || !isPickFqn(kind, fqn, acceptsUserSkill)) {
    return null;
  }
  return { kind, fqn };
}

/** Validate a client-sent picks list; the first problem found is reported. */
export function parseSelectedResources(
  raw: unknown,
  acceptsUserSkill?: SkillFqnCheck,
): ParseResult {
  if (!Array.isArray(raw)) {
    return { ok: false, error: 'resources must be an array' };
  }
  if (raw.length > MAX_SELECTIONS) {
    return { ok: false, error: `at most ${MAX_SELECTIONS} resources` };
  }
  const resources: SelectedResource[] = [];
  const seen = new Set<string>();
  for (const [index, item] of raw.entries()) {
    const resource = parseResource(item, acceptsUserSkill);
    if (resource === null) {
      return { ok: false, error: `resources[${index}] is not a valid pick` };
    }
    const key = `${resource.kind}:${resource.fqn}`;
    if (seen.has(key)) {
      return { ok: false, error: `resources[${index}] is a duplicate` };
    }
    seen.add(key);
    resources.push(resource);
  }
  return { ok: true, resources };
}

/** Process-local picks for apps without Lakebase; lost on restart. */
export class MemoryPreferenceStore implements PreferenceStore {
  private readonly picks = new Map<string, SelectedResource[]>();

  async get(userId: string): Promise<SelectedResource[]> {
    return this.picks.get(userId) ?? [];
  }

  async put(userId: string, resources: SelectedResource[]): Promise<void> {
    this.picks.set(userId, [...resources]);
  }
}
