/**
 * Agent Bricks: the skill form model (`skills.gateway.authoring`, or
 * Lakebase user skills when `authoring.store` is `lakebase`).
 *
 * Mirrors the agent's checks in `runtime/skill_authoring.py` (and the chat
 * UI server's `server/src/lib/user-skills.ts` for Lakebase) so the form can
 * explain a problem before submitting; the agent still decides.  The body
 * limit is approximate because the agent counts the whole `SKILL.md`,
 * frontmatter included.
 *
 * Kept free of runtime imports so `payloads/agent/ui/tests/ts/` can test it.
 */

import type { UserSkillSaved } from '@chat-template/core';
import { canAdd, withPick, type Pick, type UserCatalogLimits } from './resource-picks.ts';

export const SLUG_PATTERN = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;
export const MAX_SLUG_CHARS = 40;
export const MAX_DESCRIPTION_CHARS = 1024;

export type AuthoringUnavailableReason = 'apps_unity_catalog_scope';

export type AuthoringStore = 'unity_catalog' | 'lakebase';

export interface AuthoringInfo {
  schema_fqn: string;
  max_skill_md_chars: number;
  unavailable_reason: AuthoringUnavailableReason | null;
  store: AuthoringStore;
}

/** The toast for one skill the agent saved (or could not save) for the user. */
export function skillSavedText(saved: UserSkillSaved): string {
  if (!saved.ok) {
    const name = saved.slug ? ` ${saved.slug}` : '';
    return `The agent could not save the skill${name}: ${saved.error}`;
  }
  const verb = saved.created ? 'Created' : 'Updated';
  return `${verb} skill ${saved.display_name}; it is picked from your next message.`;
}

/** Where the form says the skill goes. */
export function savedInText(authoring: AuthoringInfo): string {
  return authoring.store === 'lakebase'
    ? 'Saved to your account; only you can see it.'
    : `Saved in ${authoring.schema_fqn} as you.`;
}

export const AUTHORING_UNAVAILABLE_TEXT: Record<AuthoringUnavailableReason, string> = {
  apps_unity_catalog_scope: 'Creating your own skills is currently not available in this app.',
};

export interface SkillDraft {
  slug: string;
  description: string;
  body: string;
}

export interface AuthoredSkill {
  fqn: string;
  display_name: string;
  description: string;
}

/** The fields an edit may change; the name is the skill's id. */
export type SkillEdit = Omit<SkillDraft, 'slug'>;

export type DraftField = keyof SkillDraft;
export type DraftErrors = Partial<Record<DraftField, string>>;

function slugError(slug: string): string | undefined {
  if (slug.length > MAX_SLUG_CHARS) {
    return `At most ${MAX_SLUG_CHARS} characters.`;
  }
  if (!SLUG_PATTERN.test(slug)) {
    return 'Lower-case letters and digits, separated by single hyphens.';
  }
  return undefined;
}

function descriptionError(description: string): string | undefined {
  if (description.trim() === '') {
    return 'Say when the agent should use this skill.';
  }
  if (description.length > MAX_DESCRIPTION_CHARS) {
    return `At most ${MAX_DESCRIPTION_CHARS} characters.`;
  }
  return undefined;
}

function bodyError(body: string, maxChars: number): string | undefined {
  if (body.trim() === '') {
    return 'Write the instructions.';
  }
  if (body.length > maxChars) {
    return `At most ${maxChars} characters.`;
  }
  return undefined;
}

/** Per-field problems; empty when the draft can be submitted. */
export function validateDraft(
  draft: SkillDraft,
  authoring: AuthoringInfo,
): DraftErrors {
  const errors: DraftErrors = {
    slug: slugError(draft.slug),
    description: descriptionError(draft.description),
    body: bodyError(draft.body, authoring.max_skill_md_chars),
  };
  return Object.fromEntries(
    Object.entries(errors).filter(([, message]) => message !== undefined),
  ) as DraftErrors;
}

/**
 * Picks after creating *skill*: a Unity Catalog skill is added when the
 * limits leave room; a Lakebase skill waits in the catalog until picked.
 */
export function autoSelectNewSkill(
  picks: Pick[],
  skill: AuthoredSkill,
  limits: UserCatalogLimits,
  store: AuthoringStore,
): Pick[] {
  if (store === 'lakebase' || !canAdd('skill', picks, limits)) {
    return picks;
  }
  return withPick(picks, { kind: 'skill', fqn: skill.fqn });
}
