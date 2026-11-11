/**
 * Agent Bricks: user skills stored in Lakebase (#642,
 * `spec.runtime.tools.skills.user_skills.store: lakebase`).
 *
 * A user skill is addressed as `lakebase:<slug>` wherever a resource FQN is
 * expected (picks, the resources panel, `/api/agent/skills/:fqn`); the
 * prefix cannot occur in a Unity Catalog name.  The rules mirror
 * `SkillDraft` / `SkillEdit` in `runtime/skill_authoring.py`.
 *
 * Kept free of runtime imports so `payloads/agent/ui/tests/ts/` can test it.
 */

export const USER_SKILL_PREFIX = 'lakebase:';
export const SLUG_PATTERN = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;
export const MAX_SLUG_CHARS = 40;
export const MAX_DESCRIPTION_CHARS = 1024;

export interface UserSkillLimits {
  maxSkills: number;
  maxBodyChars: number;
}

export interface SkillEdit {
  description: string;
  body: string;
}

export interface SkillDraft extends SkillEdit {
  slug: string;
}

export interface StoredSkill extends SkillDraft {
  updatedAt: Date;
}

/** The `AuthoredSkill` shape the New skill form expects back. */
export interface AuthoredSkill {
  fqn: string;
  display_name: string;
  description: string;
}

export interface UserSkillView extends AuthoredSkill {
  body: string;
}

export type Parsed<T> = { ok: true; value: T } | { ok: false; error: string };

export function userSkillFqn(slug: string): string {
  return `${USER_SKILL_PREFIX}${slug}`;
}

/** The slug of a `lakebase:<slug>` FQN, or `undefined` for anything else. */
export function slugFromFqn(fqn: string): string | undefined {
  if (!fqn.startsWith(USER_SKILL_PREFIX)) {
    return undefined;
  }
  const slug = fqn.slice(USER_SKILL_PREFIX.length);
  return isSlug(slug) ? slug : undefined;
}

export function isUserSkillFqn(fqn: string): boolean {
  return slugFromFqn(fqn) !== undefined;
}

export function isSlug(value: string): boolean {
  return value.length <= MAX_SLUG_CHARS && SLUG_PATTERN.test(value);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function onlyKeys(raw: Record<string, unknown>, allowed: string[]): boolean {
  return Object.keys(raw).every((key) => allowed.includes(key));
}

function editError(raw: Record<string, unknown>, maxBodyChars: number): string | undefined {
  const { description, body } = raw;
  if (typeof description !== 'string' || description.trim() === '') {
    return 'description is required';
  }
  if (description.length > MAX_DESCRIPTION_CHARS) {
    return `description must be at most ${MAX_DESCRIPTION_CHARS} characters`;
  }
  if (typeof body !== 'string' || body.trim() === '') {
    return 'body is required';
  }
  if (body.length > maxBodyChars) {
    return `body has ${body.length} characters, over the ${maxBodyChars} limit`;
  }
  return undefined;
}

export function parseSkillEdit(raw: unknown, maxBodyChars: number): Parsed<SkillEdit> {
  if (!isRecord(raw) || !onlyKeys(raw, ['description', 'body'])) {
    return { ok: false, error: 'expected { description, body }' };
  }
  const error = editError(raw, maxBodyChars);
  if (error) {
    return { ok: false, error };
  }
  return { ok: true, value: { description: raw.description as string, body: raw.body as string } };
}

export function parseSkillDraft(raw: unknown, maxBodyChars: number): Parsed<SkillDraft> {
  if (!isRecord(raw) || !onlyKeys(raw, ['slug', 'description', 'body'])) {
    return { ok: false, error: 'expected { slug, description, body }' };
  }
  if (typeof raw.slug !== 'string' || !isSlug(raw.slug)) {
    return {
      ok: false,
      error: `slug must be lower-case letters and digits separated by single hyphens, at most ${MAX_SLUG_CHARS} characters`,
    };
  }
  const error = editError(raw, maxBodyChars);
  if (error) {
    return { ok: false, error };
  }
  return {
    ok: true,
    value: { slug: raw.slug, description: raw.description as string, body: raw.body as string },
  };
}

export function toAuthoredSkill(skill: SkillDraft): AuthoredSkill {
  return { fqn: userSkillFqn(skill.slug), display_name: skill.slug, description: skill.description };
}

export function toUserSkillView(skill: SkillDraft): UserSkillView {
  return { ...toAuthoredSkill(skill), body: skill.body };
}

/** The agent's tool that creates or updates one of the user's skills. */
export const SAVE_SKILL_TOOL = '_save_skill';

/** The tool calls and results of one AI SDK step, as `streamText` reports them. */
export interface StepToolCalls {
  toolCalls: ReadonlyArray<{ toolCallId: string; toolName: string; input: unknown }>;
  toolResults: ReadonlyArray<{ toolCallId: string; output: unknown }>;
}

function isToolError(output: unknown): boolean {
  return typeof output === 'string' && output.startsWith('Error:');
}

function parseToolInput(input: unknown): unknown {
  if (typeof input !== 'string') {
    return input;
  }
  try {
    return JSON.parse(input);
  } catch {
    return input;
  }
}

/**
 * The arguments of the agent's `_save_skill` calls, in order, except those
 * its own check refused (the model was told and may retry).  Still
 * untrusted: callers parse each with {@link parseSkillDraft}.
 */
export function acceptedSkillSaves(steps: readonly StepToolCalls[]): unknown[] {
  const refused = new Set(
    steps
      .flatMap((step) => step.toolResults)
      .filter((result) => isToolError(result.output))
      .map((result) => result.toolCallId),
  );
  return steps
    .flatMap((step) => step.toolCalls)
    .filter((call) => call.toolName === SAVE_SKILL_TOOL && !refused.has(call.toolCallId))
    .map((call) => parseToolInput(call.input));
}

export interface PickRef {
  kind: string;
  fqn: string;
}

/**
 * Lakebase skill slugs apart from every other pick.  Anything under the
 * `lakebase:` prefix that is not a valid skill pick is dropped, so it never
 * reaches the agent's Unity Catalog resolver.
 */
export function splitUserSkillPicks<T extends PickRef>(
  picks: readonly T[],
): { others: T[]; slugs: string[] } {
  const others: T[] = [];
  const slugs: string[] = [];
  for (const pick of picks) {
    if (!pick.fqn.startsWith(USER_SKILL_PREFIX)) {
      others.push(pick);
      continue;
    }
    const slug = slugFromFqn(pick.fqn);
    if (pick.kind === 'skill' && slug !== undefined) {
      slugs.push(slug);
    }
  }
  return { others, slugs };
}
