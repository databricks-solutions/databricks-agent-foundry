import type { ChatMessage } from '@chat-template/core';

/**
 * Result of {@link sanitizeToolPairs}.
 *
 * `messages` is the cleaned list ready to hand to `convertToModelMessages`.
 * `droppedDuplicates` carries the `toolCallId`s of tool parts that were
 * removed because the same id appeared on more than one part across the
 * conversation; callers typically log a single warning summarising the
 * count.
 */
export interface SanitizeToolPairsResult {
  messages: ChatMessage[];
  droppedDuplicates: string[];
}

// AI SDK v5 emits two shapes for a tool invocation, both unified onto
// a single UI message part:
//   { type: 'tool-<name>',  toolCallId, state, input?, output? }
//   { type: 'dynamic-tool', toolName, toolCallId, state, input?, output? }
//
// The `tool-<name>` shape is used when the tool is statically known
// to the chat client at build time; `dynamic-tool` is used for
// dynamically registered tools (notably MCP-backed tools, which is
// what the Agent Bricks chat surface emits in production).  Both
// shapes carry the same `toolCallId` + `state` contract, so the
// dedupe logic below treats them uniformly.
//
// `state` progresses through 'input-streaming' -> 'input-available'
// -> 'output-available' | 'output-error'. `convertToModelMessages`
// already drops parts stuck at 'input-streaming' / 'input-available'
// when called with `{ ignoreIncompleteToolCalls: true }`, so this
// helper only addresses the second failure mode the SDK does not
// guard against: the same `toolCallId` appearing on more than one
// part across the conversation. When that happens the wire format
// emits duplicate `tool_use` blocks with the same id and the model
// rejects the request with the exact error from issue #297:
//   "unexpected tool_use_id found in tool_result blocks"
type ToolUIPart = {
  type: `tool-${string}` | 'dynamic-tool';
  toolCallId: string;
  state:
    | 'input-streaming'
    | 'input-available'
    | 'output-available'
    | 'output-error';
};

// Higher = more resolved. We keep the highest-ranked part when an id
// appears more than once and drop the rest.
const STATE_RANK: Record<ToolUIPart['state'], number> = {
  'input-streaming': 0,
  'input-available': 1,
  'output-error': 2,
  'output-available': 3,
};

function isToolPart(part: unknown): part is ToolUIPart {
  if (typeof part !== 'object' || part === null) return false;
  const candidate = part as { type?: unknown; toolCallId?: unknown };
  if (typeof candidate.type !== 'string') return false;
  if (typeof candidate.toolCallId !== 'string') return false;
  // Both AI SDK v5 tool-part shapes share the toolCallId contract -- see
  // the type comment above for why ``dynamic-tool`` is recognised here.
  return candidate.type.startsWith('tool-') || candidate.type === 'dynamic-tool';
}

/**
 * Pick, for every duplicated `toolCallId`, the (message-index, part-index)
 * coordinate of the part we keep. Ids that appear only once are absent
 * from the returned map; callers fall through to "keep" by default.
 */
function pickWinners(messages: ChatMessage[]): Map<string, string> {
  const winners = new Map<string, { rank: number; coord: string }>();

  messages.forEach((message, mi) => {
    message.parts.forEach((part, pi) => {
      if (!isToolPart(part)) return;
      const rank = STATE_RANK[part.state] ?? -1;
      const existing = winners.get(part.toolCallId);
      // Strictly greater wins; ties keep the first occurrence so the
      // sanitiser is deterministic and order-stable.
      if (!existing || rank > existing.rank) {
        winners.set(part.toolCallId, { rank, coord: `${mi}:${pi}` });
      }
    });
  });

  const out = new Map<string, string>();
  for (const [id, value] of winners) {
    out.set(id, value.coord);
  }
  return out;
}

/**
 * De-duplicate tool-invocation UI parts by `toolCallId`.
 *
 * For every `toolCallId` that appears on more than one part across the
 * conversation, keep only the part with the most-resolved `state`
 * (`output-available` > `output-error` > `input-available` >
 * `input-streaming`) and drop the rest. Ties keep the first occurrence.
 *
 * Parts whose id is unique pass through untouched. Non-tool parts
 * (text, reasoning, file, ...) are never touched. Messages that end
 * up with zero parts after filtering are dropped from the list.
 *
 * Note: this helper does NOT drop unresolved tool parts (state
 * `input-streaming` / `input-available`). The AI SDK does that for us
 * when `convertToModelMessages` is called with
 * `{ ignoreIncompleteToolCalls: true }`; we rely on that flag at the
 * single call site in `chat-turn.ts`.
 *
 * Pure function, no I/O. See `bricks/agent_runtime/core.py` for the
 * sibling Python sanitizer that protects the agent runtime path.
 */
export function sanitizeToolPairs(
  messages: ChatMessage[],
): SanitizeToolPairsResult {
  const winners = pickWinners(messages);
  const droppedDuplicates: string[] = [];
  const cleaned: ChatMessage[] = [];

  messages.forEach((message, mi) => {
    const keptParts = message.parts.filter((part, pi) => {
      if (!isToolPart(part)) return true;
      const winnerCoord = winners.get(part.toolCallId);
      // Unique ids have a winner whose coord is this exact part; the
      // check below also covers them. Ids never seen are impossible
      // here because pickWinners scans every tool part.
      const isWinner = winnerCoord === `${mi}:${pi}`;
      if (!isWinner) {
        droppedDuplicates.push(part.toolCallId);
      }
      return isWinner;
    });

    if (keptParts.length === 0) return;
    if (keptParts.length === message.parts.length) {
      cleaned.push(message);
      return;
    }
    cleaned.push({ ...message, parts: keptParts });
  });

  return { messages: cleaned, droppedDuplicates };
}

/** The `toolCallId` of every tool part in *messages*. */
export function toolCallIds(messages: ChatMessage[]): Set<string> {
  const ids = new Set<string>();
  for (const message of messages) {
    for (const part of message.parts) {
      if (isToolPart(part)) {
        ids.add(part.toolCallId);
      }
    }
  }
  return ids;
}
