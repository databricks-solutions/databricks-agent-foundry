import {
  Router,
  type Request,
  type Response,
  type Router as RouterType,
} from 'express';
import { authMiddleware, requireAuth } from '../middleware/auth';
import { isTopic, parseFactBody, parseMemoryQuery, searchFacts } from '../lib/agent-memory';
import {
  TopicFullError,
  agentMemoryEnabled,
  agentMemoryStore,
  maxTopicChars,
  type AgentMemoryStore,
} from '../lib/agent-memory-store';

/**
 * Agent Bricks: the signed-in user's long-term agent memory (#787).
 *
 *   GET  /api/agent/memory/topics         -> { topics: [{ topic, facts }] }
 *   GET  /api/agent/memory/topics/:topic  -> { topic, facts: [string] }
 *   POST /api/agent/memory/remember       { topic, fact } -> 201 created | 200 already saved
 *   POST /api/agent/memory/forget         { topic, fact } -> 204 | 404
 *   POST /api/agent/memory/search         { query }       -> { hits: [{ topic, fact }] }
 *
 * The agent's memory tools call these with the user's forwarded identity
 * (`runtime/chat_server.py`).  Gated on ENABLE_AGENT_MEMORY; errors use
 * the agent's `{ detail }` shape.
 */
export const agentMemoryRouter: RouterType = Router();

const MEMORY_PATH = '/agent/memory';
const auth = [authMiddleware, requireAuth];

function fail(res: Response, status: number, detail: string): null {
  res.status(status).json({ detail });
  return null;
}

/** The caller's user id and the store, or `null` once the request has been answered. */
function memoryContext(
  req: Request,
  res: Response,
): { userId: string; store: AgentMemoryStore } | null {
  if (!agentMemoryEnabled()) {
    return fail(res, 404, 'Agent memory is not enabled.');
  }
  const userId = req.session?.user.id;
  if (!userId) {
    return fail(res, 401, 'Sign in to use the agent memory.');
  }
  const store = agentMemoryStore();
  if (!store) {
    return fail(res, 503, 'Agent memory needs the chat history database.');
  }
  return { userId, store };
}

agentMemoryRouter.get(`${MEMORY_PATH}/topics`, ...auth, async (req: Request, res: Response) => {
  const context = memoryContext(req, res);
  if (!context) return;
  try {
    return res.status(200).json({ topics: await context.store.topics(context.userId) });
  } catch (error) {
    console.error('[agent-memory] topics failed:', error);
    return fail(res, 503, 'Could not load the memory.');
  }
});

agentMemoryRouter.get(
  `${MEMORY_PATH}/topics/:topic`,
  ...auth,
  async (req: Request, res: Response) => {
    const context = memoryContext(req, res);
    if (!context) return;
    const { topic } = req.params;
    if (!isTopic(topic)) {
      return fail(res, 400, 'Unknown topic.');
    }
    try {
      const facts = await context.store.facts(context.userId, topic);
      return res.status(200).json({ topic, facts: facts.map((entry) => entry.fact) });
    } catch (error) {
      console.error('[agent-memory] read failed:', error);
      return fail(res, 503, 'Could not load the memory.');
    }
  },
);

agentMemoryRouter.post(`${MEMORY_PATH}/remember`, ...auth, async (req: Request, res: Response) => {
  const context = memoryContext(req, res);
  if (!context) return;
  const maxChars = maxTopicChars();
  const parsed = parseFactBody(req.body, maxChars);
  if ('error' in parsed) {
    return fail(res, 400, parsed.error);
  }
  try {
    const status = await context.store.remember(context.userId, parsed, maxChars);
    return res.status(status === 'created' ? 201 : 200).json({ status });
  } catch (error) {
    if (error instanceof TopicFullError) {
      return fail(res, 409, error.message);
    }
    console.error('[agent-memory] remember failed:', error);
    return fail(res, 503, 'Could not save the memory.');
  }
});

agentMemoryRouter.post(`${MEMORY_PATH}/forget`, ...auth, async (req: Request, res: Response) => {
  const context = memoryContext(req, res);
  if (!context) return;
  const parsed = parseFactBody(req.body, maxTopicChars());
  if ('error' in parsed) {
    return fail(res, 400, parsed.error);
  }
  try {
    if (!(await context.store.forget(context.userId, parsed))) {
      return fail(res, 404, 'No such memory.');
    }
    return res.status(204).end();
  } catch (error) {
    console.error('[agent-memory] forget failed:', error);
    return fail(res, 503, 'Could not update the memory.');
  }
});

agentMemoryRouter.post(`${MEMORY_PATH}/search`, ...auth, async (req: Request, res: Response) => {
  const context = memoryContext(req, res);
  if (!context) return;
  const query = parseMemoryQuery(req.body);
  if (query === null) {
    return fail(res, 400, 'query must be non-empty text.');
  }
  try {
    const facts = await context.store.facts(context.userId);
    return res.status(200).json({ hits: searchFacts(facts, query) });
  } catch (error) {
    console.error('[agent-memory] search failed:', error);
    return fail(res, 503, 'Could not search the memory.');
  }
});
