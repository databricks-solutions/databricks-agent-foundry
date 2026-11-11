import { envFlag } from './env-flag';
import { picksForRequest } from './preference-store';
import type { SelectedResource } from './resource-preferences';
import { userSkillStore } from './user-skill-store';
import {
  splitUserSkillPicks,
  toUserSkillView,
  type UserSkillView,
} from './user-skills';

/**
 * Agent Bricks: what the user picked in the resources panel, as sent with
 * one agent request.  `resources` become `custom_inputs.selected_resources`
 * (Unity Catalog names only); `userSkills` are the picked Lakebase skills
 * that still exist, sent with their bodies as `custom_inputs.user_skills`.
 */
export interface RequestPicks {
  resources: SelectedResource[] | null;
  userSkills: UserSkillView[] | null;
}

async function pickedUserSkills(
  userId: string,
  slugs: string[],
): Promise<UserSkillView[] | null> {
  const store = envFlag('ENABLE_USER_SKILLS') ? userSkillStore() : null;
  if (store === null || slugs.length === 0) {
    return null;
  }
  try {
    const skills = await store.getMany(userId, slugs);
    return skills.length > 0 ? skills.map(toUserSkillView) : null;
  } catch (error) {
    console.error('[user-skills] read failed; chatting without them:', error);
    return null;
  }
}

/** Never rejects: a failed read means chatting without those picks. */
export async function requestPicks(userId: string): Promise<RequestPicks> {
  const { others, slugs } = splitUserSkillPicks((await picksForRequest(userId)) ?? []);
  return {
    resources: others.length > 0 ? others : null,
    userSkills: await pickedUserSkills(userId, slugs),
  };
}
