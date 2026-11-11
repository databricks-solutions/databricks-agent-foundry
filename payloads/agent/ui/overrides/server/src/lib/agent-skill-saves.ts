import type { UserSkillSaved } from '@chat-template/core';
import { envFlag } from './env-flag';
import { preferenceStore } from './preference-store';
import { MAX_SELECTIONS } from './resource-preferences';
import {
  SkillLimitError,
  userSkillLimits,
  userSkillStore,
  type UserSkillStore,
} from './user-skill-store';
import {
  acceptedSkillSaves,
  parseSkillDraft,
  toAuthoredSkill,
  userSkillFqn,
  type StepToolCalls,
} from './user-skills';

/**
 * Agent Bricks: saves the agent's `_save_skill` calls as the signed-in user
 * (#642).  The agent only checks and accepts a draft; this runs after its
 * response has streamed, re-checks each draft, creates or updates the
 * skill and picks it.  The agent can never delete a skill.
 */

function slugOf(input: unknown): string | null {
  const slug = (input as { slug?: unknown } | null)?.slug;
  return typeof slug === 'string' ? slug : null;
}

async function pickSkill(userId: string, fqn: string): Promise<void> {
  try {
    const picks = await preferenceStore().get(userId);
    const picked = picks.some((pick) => pick.kind === 'skill' && pick.fqn === fqn);
    if (!picked && picks.length < MAX_SELECTIONS) {
      await preferenceStore().put(userId, [...picks, { kind: 'skill', fqn }]);
    }
  } catch (error) {
    console.warn('[user-skills] saved skill could not be picked:', error);
  }
}

async function saveOne(
  store: UserSkillStore,
  userId: string,
  input: unknown,
): Promise<UserSkillSaved> {
  const { maxSkills, maxBodyChars } = userSkillLimits();
  const parsed = parseSkillDraft(input, maxBodyChars);
  if (!parsed.ok) {
    return { ok: false, slug: slugOf(input), error: parsed.error };
  }
  let outcome: 'created' | 'updated';
  try {
    outcome = await store.save(userId, parsed.value, maxSkills);
  } catch (error) {
    if (error instanceof SkillLimitError) {
      return { ok: false, slug: parsed.value.slug, error: error.message };
    }
    console.error('[user-skills] agent save failed:', error);
    return { ok: false, slug: parsed.value.slug, error: 'Could not save the skill.' };
  }
  await pickSkill(userId, userSkillFqn(parsed.value.slug));
  return { ok: true, ...toAuthoredSkill(parsed.value), created: outcome === 'created' };
}

/** One outcome per accepted `_save_skill` call; never rejects. */
export async function saveAgentSkills(
  userId: string,
  steps: PromiseLike<readonly StepToolCalls[]>,
): Promise<UserSkillSaved[]> {
  if (!envFlag('ENABLE_USER_SKILLS')) {
    return [];
  }
  let inputs: unknown[];
  try {
    inputs = acceptedSkillSaves(await steps);
  } catch (error) {
    console.warn('[user-skills] could not read the agent tool calls:', error);
    return [];
  }
  const store = userSkillStore();
  if (inputs.length === 0 || store === null) {
    return [];
  }
  const outcomes: UserSkillSaved[] = [];
  for (const input of inputs) {
    // In order, so a later save of the same slug wins and the limit holds.
    outcomes.push(await saveOne(store, userId, input));
  }
  return outcomes;
}
