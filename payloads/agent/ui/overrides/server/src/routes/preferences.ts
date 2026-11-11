import {
  Router,
  type Request,
  type Response,
  type Router as RouterType,
} from 'express';
import { authMiddleware, requireAuth } from '../middleware/auth';
import { envFlag } from '../lib/env-flag';
import { preferenceStore, userSkillPickCheck } from '../lib/preference-store';
import { parseSelectedResources } from '../lib/resource-preferences';

/**
 * Agent Bricks: the signed-in user's sidebar picks.
 *
 *   GET /api/preferences/resources -> { resources: [{ kind, fqn }] }
 *   PUT /api/preferences/resources    { resources: [{ kind, fqn }] }
 *
 * Gated on ENABLE_RESOURCES_SIDEBAR.  Picks are keyed by the session's
 * user id, so a user can only ever read or replace their own.
 */
export const preferencesRouter: RouterType = Router();

preferencesRouter.use(authMiddleware);

/** The caller's user id, or `null` once the request has been answered. */
function sidebarUser(req: Request, res: Response): string | null {
  if (!envFlag('ENABLE_RESOURCES_SIDEBAR')) {
    res.status(501).json({ error: 'The resources sidebar is not enabled.' });
    return null;
  }
  const userId = req.session?.user.id;
  if (!userId) {
    res.status(401).json({ error: 'Sign in to keep picks.' });
    return null;
  }
  return userId;
}

preferencesRouter.get(
  '/resources',
  requireAuth,
  async (req: Request, res: Response) => {
    const userId = sidebarUser(req, res);
    if (userId === null) return;
    try {
      const resources = await preferenceStore().get(userId);
      return res.status(200).json({ resources });
    } catch (error) {
      console.error('[preferences] read failed:', error);
      return res.status(503).json({ error: 'Could not load your picks.' });
    }
  },
);

preferencesRouter.put(
  '/resources',
  requireAuth,
  async (req: Request, res: Response) => {
    const userId = sidebarUser(req, res);
    if (userId === null) return;
    const parsed = parseSelectedResources(req.body?.resources, userSkillPickCheck());
    if (!parsed.ok) {
      return res.status(400).json({ error: parsed.error });
    }
    try {
      await preferenceStore().put(userId, parsed.resources);
      return res.status(200).json({ resources: parsed.resources });
    } catch (error) {
      console.error('[preferences] write failed:', error);
      return res.status(503).json({ error: 'Could not save your picks.' });
    }
  },
);
