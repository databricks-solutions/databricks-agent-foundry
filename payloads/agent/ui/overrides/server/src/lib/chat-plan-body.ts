/**
 * Agent Bricks: the agent plan of one chat (`spec.runtime.context.plan`) as
 * the chat UI server stores it: the open plan's markdown (empty without one)
 * and the chat's finished plans.  Kept free of runtime imports so
 * `payloads/agent/ui/tests/ts/` can test it; the table lives in
 * `chat-plan-store.ts`.
 */

export interface StoredPlan {
  content: string;
  history: string;
}

/** Mirrors the default of AgentPlanRuntime.max_chars in payloads/agent/contracts.py. */
export const DEFAULT_PLAN_MAX_CHARS = 8000;

/**
 * The agent's `PUT` body as the plan to store, or `null` when it is not two
 * strings of at most *maxChars* each.  The agent checks the same limits; this
 * keeps a direct call from storing anything larger.
 */
export function parsePlanBody(body: unknown, maxChars: number): StoredPlan | null {
  if (typeof body !== 'object' || body === null) {
    return null;
  }
  const { content, history } = body as Record<string, unknown>;
  if (typeof content !== 'string' || typeof history !== 'string') {
    return null;
  }
  if (content.length > maxChars || history.length > maxChars) {
    return null;
  }
  return { content, history };
}
