import {
  Router,
  type Request,
  type Response,
  type Router as RouterType,
} from 'express';
import { checkChatAccess } from '@chat-template/core';
import { authMiddleware, getIdFromRequest, requireAuth } from '../middleware/auth';
import { parsePlanBody } from '../lib/chat-plan-body';
import { type ChatPlanStore, chatPlanEnabled, chatPlanStore, planMaxChars } from '../lib/chat-plan-store';

/**
 * Agent Bricks: the agent's plan of one chat (`spec.runtime.context.plan`),
 * kept in the chat-history Lakebase.
 *
 *   GET /api/chat/:id/plan -> { content: string | null, history: string }
 *   PUT /api/chat/:id/plan    { content, history } -> 204
 *
 * Callers: the agent server over localhost (one `GET` per request, one
 * `PUT` per change) and the plan panel above the chat input (`content`).
 *
 * Access: the forwarded user identity only selects whose chat this is; a
 * caller who does not own the chat gets 403.  A chat not saved yet (opened,
 * no message sent) reads as having no plan; writing to it is refused.  This server reads and writes
 * Lakebase as the App's service principal, like the rest of the chat
 * history, so users never get database access.
 *
 * Returns 404 while the plan is off and 503 without Lakebase.
 */
export const chatPlanRouter: RouterType = Router();

chatPlanRouter.use(authMiddleware);

interface OwnedPlan {
  store: ChatPlanStore;
  chatId: string;
  saved: boolean;
}

/**
 * The store and this chat when the plan is on and the chat is the caller's
 * (or, with *allowUnsaved*, not saved yet); otherwise answers and resolves to `null`.
 */
async function ownedPlan(
  req: Request,
  res: Response,
  { allowUnsaved }: { allowUnsaved: boolean },
): Promise<OwnedPlan | null> {
  if (!chatPlanEnabled()) {
    res.status(404).json({ error: 'The agent plan is not enabled.' });
    return null;
  }
  const store = chatPlanStore();
  if (!store) {
    res.status(503).json({ error: 'The plan needs the chat history database.' });
    return null;
  }
  const chatId = getIdFromRequest(req);
  if (!chatId) return null;
  const { chat, reason } = await checkChatAccess(chatId, req.session?.user.id);
  if (allowUnsaved && reason === 'not_found') {
    return { store, chatId, saved: false };
  }
  if (!chat || chat.userId !== req.session?.user.id) {
    res.status(403).json({ error: 'Only the owner can use the plan of this chat.' });
    return null;
  }
  return { store, chatId, saved: true };
}

chatPlanRouter.get('/:id/plan', requireAuth, async (req: Request, res: Response) => {
  const owned = await ownedPlan(req, res, { allowUnsaved: true });
  if (!owned) return;
  if (!owned.saved) {
    return res.status(200).json({ content: null, history: '' });
  }
  try {
    const plan = await owned.store.get(owned.chatId);
    return res.status(200).json({ content: plan.content || null, history: plan.history });
  } catch (error) {
    console.error(`[chat-plan] read chat_id=${owned.chatId} failed:`, error);
    return res.status(503).json({ error: 'Could not load the plan.' });
  }
});

chatPlanRouter.put('/:id/plan', requireAuth, async (req: Request, res: Response) => {
  const owned = await ownedPlan(req, res, { allowUnsaved: false });
  if (!owned) return;
  const plan = parsePlanBody(req.body, planMaxChars());
  if (!plan) {
    return res.status(400).json({ error: 'Send content and history as strings within the plan size limit.' });
  }
  try {
    await owned.store.put(owned.chatId, plan);
    return res.status(204).end();
  } catch (error) {
    console.error(`[chat-plan] write chat_id=${owned.chatId} failed:`, error);
    return res.status(503).json({ error: 'Could not save the plan.' });
  }
});
