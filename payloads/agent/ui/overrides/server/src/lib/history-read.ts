/**
 * Agent Bricks: read a chat's summarized turns (#685), the pure half.
 *
 * Search only finds what the agent knows to ask for; reading lets it look
 * around.  `outlineHistory` lists the turns the summary replaced, each with
 * its tool calls and where they start, and `readHistory` returns one page of
 * a turn's text from an offset, the same offsets search hits carry.  The
 * input is `summarizedTurns` (see `compaction.ts`).  No I/O and only type
 * imports, so it is unit-tested out of tree at `payloads/agent/ui/tests/ts/`.
 */
import type { TranscriptTurn } from './compaction';

export const PAGE_CHARS = 8_000;
export const REQUEST_PREVIEW_CHARS = 160;
// Bounds the outline in a chat with many tool calls; the oldest are listed.
export const MAX_OUTLINE_TOOL_CALLS = 100;

export interface ToolCallOutline {
  /** The tool's name and the start of its input. */
  label: string;
  /** Offset of the call in its turn's text. */
  at: number;
  chars: number;
}

export interface TurnOutline {
  turn: number;
  /** The start of the user's first message in the turn; empty when the turn has none. */
  request: string;
  toolCalls: ToolCallOutline[];
  /** Tool calls past {@link MAX_OUTLINE_TOOL_CALLS}, counted but not listed. */
  moreToolCalls: number;
  chars: number;
}

export interface HistoryPage {
  turn: number;
  /** How many turns the summary replaced. */
  turns: number;
  offset: number;
  end: number;
  chars: number;
  text: string;
}

function requestPreview(turn: TranscriptTurn): string {
  const first = turn.passages.find((span) => span.kind === 'user');
  if (!first) {
    return '';
  }
  const text = turn.text.slice(first.start, first.end).replace(/\s+/g, ' ').trim();
  return text.length > REQUEST_PREVIEW_CHARS ? `${text.slice(0, REQUEST_PREVIEW_CHARS)}…` : text;
}

function toolCallsOf(turn: TranscriptTurn): ToolCallOutline[] {
  return turn.passages
    .filter((span) => span.kind === 'tool')
    .map((span) => ({ label: span.label ?? '', at: span.start, chars: span.end - span.start }));
}

/** Each summarized turn: what the user asked, its tool calls with their offsets, how long it is. */
export function outlineHistory(turns: TranscriptTurn[]): TurnOutline[] {
  let budget = MAX_OUTLINE_TOOL_CALLS;
  return turns.map((turn) => {
    const calls = toolCallsOf(turn);
    const listed = calls.slice(0, budget);
    budget -= listed.length;
    return {
      turn: turn.turn,
      request: requestPreview(turn),
      toolCalls: listed,
      moreToolCalls: calls.length - listed.length,
      chars: turn.text.length,
    };
  });
}

/**
 * Where a page from *start* ends: after the last part that ends in the
 * page's second half, else after its last line break there, else at
 * {@link PAGE_CHARS}.
 */
function pageEnd(turn: TranscriptTurn, start: number): number {
  const limit = start + PAGE_CHARS;
  if (limit >= turn.text.length) {
    return turn.text.length;
  }
  const floor = start + PAGE_CHARS / 2;
  const partEnds = turn.passages.map((span) => span.end).filter((end) => end > floor && end < limit);
  if (partEnds.length > 0) {
    return partEnds[partEnds.length - 1] + 1;
  }
  const lineBreak = turn.text.lastIndexOf('\n', limit - 1);
  return lineBreak > floor ? lineBreak + 1 : limit;
}

/** About {@link PAGE_CHARS} of *turn*'s text from *offset*; `null` when the summary did not replace it. */
export function readHistory(turns: TranscriptTurn[], turn: number, offset: number): HistoryPage | null {
  const found = turns.find((candidate) => candidate.turn === turn);
  if (!found) {
    return null;
  }
  const start = Math.min(Math.max(0, Math.floor(offset)), found.text.length);
  const end = pageEnd(found, start);
  return {
    turn,
    turns: turns.length,
    offset: start,
    end,
    chars: found.text.length,
    text: found.text.slice(start, end),
  };
}
