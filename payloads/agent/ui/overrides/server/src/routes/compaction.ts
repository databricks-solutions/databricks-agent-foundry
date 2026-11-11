import {
  Router,
  type Request,
  type Response,
  type Router as RouterType,
} from 'express';
import { checkChatAccess, convertToUIMessages } from '@chat-template/core';
import { getMessagesByChatId } from '@chat-template/db';
import {
  authMiddleware,
  getIdFromRequest,
  requireAuth,
  requireChatAccess,
} from '../middleware/auth';
import { MAX_FOCUS_CHARS, type TranscriptTurn, summarizedTurns } from '../lib/compaction';
import { type CompactionStore, compactionStore } from '../lib/compaction-store';
import { compactChat, compactionForRequest } from '../lib/compaction-runner';
import { outlineHistory, readHistory } from '../lib/history-read';
import { searchHistory } from '../lib/history-search';

/**
 * Agent Bricks: conversation compaction summaries of one chat (#637).
 *
 *   GET  /api/chat/:id/compactions    -> { compactions: [...] }, oldest first
 *   POST /api/chat/:id/compact           { focus? } -> { status, compaction? }
 *   POST /api/chat/:id/history/search    { query } -> { hits: [...] } (#685)
 *   POST /api/chat/:id/history/read      { turn?, offset? } -> { outline } | { page, turns } (#685)
 *
 * 404 while compaction is off or without Lakebase.  Reading and searching
 * need access to the chat; compacting needs to own it, like sending a
 * message.  A chat not saved yet (opened, no message sent) has no
 * summaries, so listing them answers an empty list.  The agent runtime searches and reads as the user, with the
 * user's forwarded headers, for its `_search_history` and `_read_history`
 * tools.
 */
export const compactionRouter: RouterType = Router();

const MAX_QUERY_CHARS = 500;

compactionRouter.use(authMiddleware);

/** The store, or `null` once the request has been answered. */
function storeOrNotFound(res: Response): CompactionStore | null {
  const store = compactionStore();
  if (!store) {
    res.status(404).json({ error: 'Conversation compaction is not enabled.' });
  }
  return store;
}

/** The optional focus from *body*, or an error message. */
function parseFocus(body: unknown): { focus: string | null } | { error: string } {
  const focus = (body as { focus?: unknown } | null)?.focus;
  if (focus === undefined || focus === null) {
    return { focus: null };
  }
  if (typeof focus !== 'string' || focus.length > MAX_FOCUS_CHARS) {
    return { error: `focus must be text of at most ${MAX_FOCUS_CHARS} characters.` };
  }
  return { focus: focus.trim() || null };
}

/** The query from *body*, or `null` when it is missing, empty or too long. */
function parseQuery(body: unknown): string | null {
  const query = (body as { query?: unknown } | null)?.query;
  if (typeof query !== 'string' || query.length > MAX_QUERY_CHARS || !query.trim()) {
    return null;
  }
  return query;
}

function isOptionalInteger(value: unknown, min: number): value is number | null | undefined {
  return value === undefined || value === null || (Number.isInteger(value) && (value as number) >= min);
}

/** The optional turn and offset from *body*, or an error message. */
function parseRead(body: unknown): { turn: number | null; offset: number } | { error: string } {
  const { turn, offset } = (body ?? {}) as { turn?: unknown; offset?: unknown };
  if (!isOptionalInteger(turn, 1) || !isOptionalInteger(offset, 0)) {
    return { error: 'turn must be an integer from 1 and offset an integer from 0.' };
  }
  return { turn: turn ?? null, offset: offset ?? 0 };
}

/** The turns the latest summary of chat *id* replaced. */
async function loadSummarizedTurns(id: string): Promise<TranscriptTurn[]> {
  const [messages, compaction] = await Promise.all([
    getMessagesByChatId({ id }),
    compactionForRequest(id),
  ]);
  return summarizedTurns(convertToUIMessages(messages), compaction);
}

compactionRouter.get(
  '/:id/compactions',
  requireAuth,
  async (req: Request, res: Response) => {
    const id = getIdFromRequest(req);
    const store = storeOrNotFound(res);
    if (!id || !store) return;
    const { allowed, reason } = await checkChatAccess(id, req.session?.user.id);
    if (reason === 'not_found') {
      return res.status(200).json({ compactions: [] });
    }
    if (!allowed) {
      return res.status(403).json({ error: 'You cannot read the summaries of this chat.' });
    }
    try {
      return res.status(200).json({ compactions: await store.listFor(id) });
    } catch (error) {
      console.error('[compaction] list failed:', error);
      return res.status(503).json({ error: 'Could not load the summaries.' });
    }
  },
);

compactionRouter.post(
  '/:id/history/search',
  [requireAuth, requireChatAccess],
  async (req: Request, res: Response) => {
    const id = getIdFromRequest(req);
    const store = storeOrNotFound(res);
    if (!id || !store) return;
    const query = parseQuery(req.body);
    if (query === null) {
      return res.status(400).json({ error: `query must be text of 1 to ${MAX_QUERY_CHARS} characters.` });
    }
    try {
      return res.status(200).json({ hits: searchHistory(await loadSummarizedTurns(id), query) });
    } catch (error) {
      console.error(`[compaction] history search chat_id=${id} failed:`, error);
      return res.status(503).json({ error: 'Could not search the conversation.' });
    }
  },
);

compactionRouter.post(
  '/:id/history/read',
  [requireAuth, requireChatAccess],
  async (req: Request, res: Response) => {
    const id = getIdFromRequest(req);
    const store = storeOrNotFound(res);
    if (!id || !store) return;
    const parsed = parseRead(req.body);
    if ('error' in parsed) {
      return res.status(400).json({ error: parsed.error });
    }
    try {
      const turns = await loadSummarizedTurns(id);
      if (parsed.turn === null) {
        return res.status(200).json({ outline: outlineHistory(turns) });
      }
      return res.status(200).json({ page: readHistory(turns, parsed.turn, parsed.offset), turns: turns.length });
    } catch (error) {
      console.error(`[compaction] history read chat_id=${id} failed:`, error);
      return res.status(503).json({ error: 'Could not read the conversation.' });
    }
  },
);

compactionRouter.post(
  '/:id/compact',
  requireAuth,
  async (req: Request, res: Response) => {
    const id = getIdFromRequest(req);
    const store = storeOrNotFound(res);
    if (!id || !store) return;
    const parsed = parseFocus(req.body);
    if ('error' in parsed) {
      return res.status(400).json({ error: parsed.error });
    }
    const { chat } = await checkChatAccess(id, req.session?.user.id);
    if (!chat || chat.userId !== req.session?.user.id) {
      return res.status(403).json({ error: 'Only the owner can compact this chat.' });
    }
    try {
      const outcome = await compactChat(store, id, {
        trigger: 'manual',
        focus: parsed.focus,
        sourceTokensEst: null,
      });
      return res.status(outcome.status === 'busy' ? 409 : 200).json(outcome);
    } catch (error) {
      const reason = error instanceof Error ? error.message : String(error);
      console.error(`[compaction] manual chat_id=${id} failed: ${reason}`);
      return res.status(502).json({ error: 'Could not summarize the conversation.' });
    }
  },
);
