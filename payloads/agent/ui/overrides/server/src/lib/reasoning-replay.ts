/**
 * Agent Bricks: signed reasoning replayed across turns (#681).
 *
 * The agent returns each model call's original content, signed reasoning
 * included, in `custom_outputs.reasoning_replay` when the request carries
 * `custom_inputs.reasoning_replay`.  The chat server stores the entries
 * and sends back those whose tool calls are still in the next request, so
 * earlier turns reach the model as they were cached.  The entries never
 * reach the browser, and the agent re-validates every one.
 */

export const REASONING_REPLAY_KEY = 'reasoning_replay';

/** Empty `content` only records which tool calls one model call made together. */
export interface ReplayEntry {
  model: string;
  tool_call_ids: string[];
  content: Record<string, unknown>[];
}

function isNonEmptyString(value: unknown): value is string {
  return typeof value === 'string' && value.length > 0;
}

/** Whether *value* has the shape of a replay entry. */
export function isReplayEntry(value: unknown): value is ReplayEntry {
  if (typeof value !== 'object' || value === null) {
    return false;
  }
  const { model, tool_call_ids: ids, content } = value as Record<string, unknown>;
  return (
    isNonEmptyString(model) &&
    Array.isArray(ids) &&
    ids.length > 0 &&
    ids.every(isNonEmptyString) &&
    Array.isArray(content) &&
    content.every((block) => typeof block === 'object' && block !== null)
  );
}

/** The well-formed entries of the agent's `custom_outputs.reasoning_replay`, or `null` when absent. */
export function replayEntriesFrom(output: unknown): ReplayEntry[] | null {
  return Array.isArray(output) ? output.filter(isReplayEntry) : null;
}

/** The entries behind any of *callIds*, in stored order. */
export function entriesForRequest(entries: ReplayEntry[], callIds: Set<string>): ReplayEntry[] {
  return entries.filter((entry) => entry.tool_call_ids.some((id) => callIds.has(id)));
}
