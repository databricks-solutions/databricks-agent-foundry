import {
  Router,
  type Request,
  type Response,
  type Router as RouterType,
} from 'express';
import { authMiddleware, requireAuth } from '../middleware/auth';
import { envFlag } from '../lib/env-flag';
import { preferenceStore } from '../lib/preference-store';
import {
  SkillExistsError,
  SkillLimitError,
  userSkillLimits,
  userSkillStore,
  type UserSkillStore,
} from '../lib/user-skill-store';
import {
  parseSkillDraft,
  parseSkillEdit,
  slugFromFqn,
  toAuthoredSkill,
  toUserSkillView,
  userSkillFqn,
} from '../lib/user-skills';

/**
 * Agent Bricks: the signed-in user's Lakebase skills (#642).
 *
 *   GET    /api/user-skills         -> { skills: [{ fqn, display_name, description, body }] }
 *   POST   /api/agent/skills           { slug, description, body } -> AuthoredSkill
 *   PUT    /api/agent/skills/:fqn      { description, body }       -> AuthoredSkill
 *   DELETE /api/agent/skills/:fqn
 *
 * Gated on ENABLE_USER_SKILLS; the agent server then leaves
 * `/api/agent/skills` to this router.  Errors use the agent's `{ detail }`
 * shape, so the New skill form shows them unchanged.
 */
export const userSkillsRouter: RouterType = Router();

const SKILLS_PATH = '/agent/skills';
const auth = [authMiddleware, requireAuth];

function fail(res: Response, status: number, detail: string): null {
  res.status(status).json({ detail });
  return null;
}

/** The caller's user id and the store, or `null` once the request has been answered. */
function skillContext(
  req: Request,
  res: Response,
): { userId: string; store: UserSkillStore } | null {
  if (!envFlag('ENABLE_USER_SKILLS')) {
    return fail(res, 404, 'User skills are not enabled.');
  }
  const userId = req.session?.user.id;
  if (!userId) {
    return fail(res, 401, 'Sign in to manage your skills.');
  }
  const store = userSkillStore();
  if (!store) {
    return fail(res, 503, 'User skills need the chat history database.');
  }
  return { userId, store };
}

function slugParam(req: Request, res: Response): string | null {
  const { fqn } = req.params;
  const slug = typeof fqn === 'string' ? slugFromFqn(fqn) : undefined;
  return slug ?? fail(res, 404, 'Unknown skill.');
}

async function forgetPick(userId: string, slug: string): Promise<void> {
  const fqn = userSkillFqn(slug);
  try {
    const picks = await preferenceStore().get(userId);
    const kept = picks.filter((pick) => pick.fqn !== fqn);
    if (kept.length !== picks.length) {
      await preferenceStore().put(userId, kept);
    }
  } catch (error) {
    console.warn('[user-skills] could not drop the deleted skill from picks:', error);
  }
}

userSkillsRouter.get('/user-skills', ...auth, async (req: Request, res: Response) => {
  const context = skillContext(req, res);
  if (!context) return;
  try {
    const skills = await context.store.list(context.userId);
    return res.status(200).json({ skills: skills.map(toUserSkillView) });
  } catch (error) {
    console.error('[user-skills] list failed:', error);
    return fail(res, 503, 'Could not load your skills.');
  }
});

userSkillsRouter.post(SKILLS_PATH, ...auth, async (req: Request, res: Response) => {
  const context = skillContext(req, res);
  if (!context) return;
  const { maxSkills, maxBodyChars } = userSkillLimits();
  const parsed = parseSkillDraft(req.body, maxBodyChars);
  if (!parsed.ok) {
    return fail(res, 400, parsed.error);
  }
  try {
    await context.store.create(context.userId, parsed.value, maxSkills);
    return res.status(201).json(toAuthoredSkill(parsed.value));
  } catch (error) {
    if (error instanceof SkillLimitError || error instanceof SkillExistsError) {
      return fail(res, 409, error.message);
    }
    console.error('[user-skills] create failed:', error);
    return fail(res, 503, 'Could not save your skill.');
  }
});

userSkillsRouter.put(`${SKILLS_PATH}/:fqn`, ...auth, async (req: Request, res: Response) => {
  const context = skillContext(req, res);
  if (!context) return;
  const slug = slugParam(req, res);
  if (slug === null) return;
  const parsed = parseSkillEdit(req.body, userSkillLimits().maxBodyChars);
  if (!parsed.ok) {
    return fail(res, 400, parsed.error);
  }
  try {
    if (!(await context.store.update(context.userId, slug, parsed.value))) {
      return fail(res, 404, 'Unknown skill.');
    }
    return res.status(200).json(toAuthoredSkill({ slug, ...parsed.value }));
  } catch (error) {
    console.error('[user-skills] update failed:', error);
    return fail(res, 503, 'Could not save your skill.');
  }
});

userSkillsRouter.delete(`${SKILLS_PATH}/:fqn`, ...auth, async (req: Request, res: Response) => {
  const context = skillContext(req, res);
  if (!context) return;
  const slug = slugParam(req, res);
  if (slug === null) return;
  try {
    if (!(await context.store.delete(context.userId, slug))) {
      return fail(res, 404, 'Unknown skill.');
    }
  } catch (error) {
    console.error('[user-skills] delete failed:', error);
    return fail(res, 503, 'Could not delete your skill.');
  }
  await forgetPick(context.userId, slug);
  return res.status(204).end();
});
