/**
 * Agent Bricks: conversation compaction, the pure half (#637).
 *
 * A compaction replaces every message before a *boundary* with a summary,
 * for the model request only; the persisted transcript is never modified.
 * The boundary is always a user message: a tool call and its result belong
 * to the assistant reply between two user messages, so cutting there never
 * separates them.  The summary is replayed as identical bytes on every later
 * turn, so it caches as part of the prompt prefix.
 *
 * No I/O and only type imports, so it is unit-tested out of tree at
 * `payloads/agent/ui/tests/ts/` (see `sanitize-tool-pairs.ts`).
 */
import type { ChatMessage, ContextWindowReading } from '@chat-template/core';

export interface CompactionView {
  boundaryMessageId: string;
  summary: string;
}

/** `custom_outputs.context_window` as the agent reports it after a turn. */
export interface ContextWindowReport {
  estimatedInputTokens: number | null;
  limitExceeded: boolean;
}

const SUMMARY_TAG_OPEN = '<conversation_summary>';
const SUMMARY_TAG_CLOSE = '</conversation_summary>';
const SUMMARY_TAG_PATTERN = /<\/?conversation_summary>/gi;
const SUMMARY_PREAMBLE =
  'The earlier part of this conversation was summarized to save space. ' +
  'Treat the summary as context, not as a new request.';

export const MAX_TEXT_CHARS = 8_000;
export const MAX_TOOL_INPUT_CHARS = 500;
export const MAX_TOOL_OUTPUT_CHARS = 1_500;
export const MAX_CHUNK_CHARS = 150_000;
export const MAX_CHUNKS = 8;
export const MAX_FOCUS_CHARS = 500;

const SUMMARY_PROMPT = [
  'You summarize the earlier part of a conversation between a user and an AI assistant',
  'so the assistant can continue it without the full transcript.',
  'Write a concise, factual summary in the language of the conversation, at most about',
  '1,500 words, as short sections or bullet points. Always keep:',
  '- the user\'s current goal and any open tasks;',
  '- decisions made and constraints or preferences the user stated;',
  '- resources in use (tables, files, tools, systems) by their exact names;',
  '- exact identifiers, numbers, URLs and error messages that may matter again;',
  '- approaches already tried and ruled out, and why;',
  '- the agreed or most likely next step.',
  'Drop pleasantries, repetition and tool output that no longer matters.',
  'Do not invent anything. Do not address the user. Output the summary only.',
].join('\n');

type Part = ChatMessage['parts'][number];

interface ToolLikePart {
  type: string;
  toolName?: string;
  state?: string;
  input?: unknown;
  output?: unknown;
  errorText?: string;
}

function isToolPart(part: { type: string }): part is ToolLikePart {
  return part.type.startsWith('tool-') || part.type === 'dynamic-tool';
}

function truncate(text: string, maxChars: number): string {
  if (text.length <= maxChars) {
    return text;
  }
  return `${text.slice(0, maxChars)} [... ${text.length - maxChars} characters cut]`;
}

function stringify(value: unknown): string {
  if (typeof value === 'string') {
    return value;
  }
  try {
    return JSON.stringify(value) ?? '';
  } catch {
    return String(value);
  }
}

/**
 * Index of the first message kept verbatim: the start of the
 * *keepRecentTurns*-th-last user turn.  `null` when there is nothing before
 * it to summarize.
 */
export function pickBoundary(
  messages: ChatMessage[],
  keepRecentTurns: number,
): number | null {
  let userTurns = 0;
  for (let index = messages.length - 1; index > 0; index -= 1) {
    if (messages[index].role !== 'user') {
      continue;
    }
    userTurns += 1;
    if (userTurns === keepRecentTurns) {
      return index;
    }
  }
  return null;
}

/** Index of the compaction's boundary in *messages*, or `null` if absent. */
export function boundaryIndex(
  messages: ChatMessage[],
  compaction: CompactionView | null,
): number | null {
  if (!compaction) {
    return null;
  }
  const index = messages.findIndex((m) => m.id === compaction.boundaryMessageId);
  return index >= 0 ? index : null;
}

/** The turns a compaction would summarize, from the previous summary's boundary on. */
export interface CompactableSpan {
  /** Index of the previous summary's boundary in the chat, or `null` without one. */
  start: number | null;
  /** The messages from that boundary on. */
  window: ChatMessage[];
  /** Index in *window* of the first message kept verbatim. */
  cut: number;
}

/** What a compaction of *messages* would summarize, or `null` while no turn is old enough. */
export function compactableSpan(
  messages: ChatMessage[],
  previous: CompactionView | null,
  keepRecentTurns: number,
): CompactableSpan | null {
  const start = boundaryIndex(messages, previous);
  const window = messages.slice(start ?? 0);
  const cut = pickBoundary(window, keepRecentTurns);
  return cut === null ? null : { start, window, cut };
}

/** The summary as the model sees it, framed so it reads as context. */
export function summaryText(summary: string): string {
  const body = summary.replace(SUMMARY_TAG_PATTERN, '').trim();
  return `${SUMMARY_TAG_OPEN}\n${SUMMARY_PREAMBLE}\n\n${body}\n${SUMMARY_TAG_CLOSE}\n\n`;
}

/**
 * The model's view of *messages* under *compaction*: the summary, then the
 * boundary message onwards.  The summary goes in front of the boundary
 * message's own parts rather than into a message of its own, so no
 * provider sees two user messages in a row.  Unchanged when there is no
 * compaction or its boundary is not in *messages*.
 */
export function applyCompaction(
  messages: ChatMessage[],
  compaction: CompactionView | null,
): ChatMessage[] {
  const index = boundaryIndex(messages, compaction);
  if (index === null || !compaction) {
    return messages;
  }
  const boundary = messages[index];
  const summaryPart = { type: 'text', text: summaryText(compaction.summary) } as Part;
  return [{ ...boundary, parts: [summaryPart, ...boundary.parts] }, ...messages.slice(index + 1)];
}

interface RenderLimits {
  text: number;
  toolInput: number;
  toolOutput: number;
}

const SUMMARIZER_LIMITS: RenderLimits = {
  text: MAX_TEXT_CHARS,
  toolInput: MAX_TOOL_INPUT_CHARS,
  toolOutput: MAX_TOOL_OUTPUT_CHARS,
};
// Search reads tool results nearly whole, since only its excerpts go back to
// the model; the cap bounds the work for a chat with huge results.
export const MAX_SEARCH_PART_CHARS = 100_000;
const SEARCH_LIMITS: RenderLimits = {
  text: MAX_SEARCH_PART_CHARS,
  toolInput: MAX_SEARCH_PART_CHARS,
  toolOutput: MAX_SEARCH_PART_CHARS,
};

function renderPart(part: Part, limits: RenderLimits): string | null {
  const candidate = part as { type: string; text?: unknown };
  if (candidate.type === 'text' && typeof candidate.text === 'string') {
    return truncate(candidate.text, limits.text);
  }
  if (candidate.type === 'file') {
    const file = part as { name?: string; filename?: string; extractedText?: string };
    const label = file.name ?? file.filename ?? 'file';
    const extract = file.extractedText ? `\n${truncate(file.extractedText, limits.toolOutput)}` : '';
    return `[Attached file: ${label}]${extract}`;
  }
  if (isToolPart(candidate)) {
    return renderToolPart(candidate, limits);
  }
  return null;
}

function toolNameOf(part: ToolLikePart): string {
  return part.toolName ?? part.type.replace(/^tool-/, '');
}

function isTextPart(value: unknown): value is { text: string } {
  return typeof value === 'object' && value !== null && typeof (value as { text?: unknown }).text === 'string';
}

function textPartsOf(value: unknown): string | null {
  return Array.isArray(value) && value.length > 0 && value.every(isTextPart)
    ? value.map((part) => part.text).join('\n')
    : null;
}

function parseJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

/**
 * A tool output as text.  MCP results arrive as a list of text parts, at
 * times JSON-encoded; their texts are used as they are, so a JSON result
 * is not escaped a second time.
 */
function toolOutputText(output: unknown): string {
  const decoded = typeof output === 'string' && output.startsWith('[') ? parseJson(output) : output;
  return textPartsOf(decoded) ?? stringify(output);
}

function renderToolPart(part: ToolLikePart, limits: RenderLimits): string {
  const input = truncate(stringify(part.input), limits.toolInput);
  const result =
    part.state === 'output-error'
      ? `error: ${part.errorText ?? 'unknown'}`
      : truncate(toolOutputText(part.output), limits.toolOutput);
  return `[Tool ${toolNameOf(part)}(${input}) -> ${result}]`;
}

function speakerOf(message: ChatMessage): string {
  return message.role === 'user' ? 'User' : 'Assistant';
}

function renderParts(message: ChatMessage, limits: RenderLimits): string[] {
  return message.parts
    .map((part) => renderPart(part, limits))
    .filter((line): line is string => line !== null && line.trim() !== '');
}

function renderMessage(message: ChatMessage, limits: RenderLimits): string | null {
  const lines = renderParts(message, limits);
  if (lines.length === 0) {
    return null;
  }
  return `${speakerOf(message)}:\n${lines.join('\n')}`;
}

const BLOCK_SEPARATOR = '\n\n';

/** Consecutive blocks grouped so each group's joined text fits {@link MAX_CHUNK_CHARS}. */
function groupBlocks(blocks: string[]): string[][] {
  const groups: string[][] = [];
  let size = 0;
  for (const block of blocks) {
    const current = groups[groups.length - 1];
    const grown = size + BLOCK_SEPARATOR.length + block.length;
    if (current && grown <= MAX_CHUNK_CHARS) {
      current.push(block);
      size = grown;
    } else {
      groups.push([block]);
      size = block.length;
    }
  }
  return groups;
}

/**
 * The summarizer's input as flat-text chunks, oldest first, each at most
 * {@link MAX_CHUNK_CHARS}: every message's text, tool calls and file
 * extracts, truncated.  A message is never split across chunks, so a tool
 * call stays with its result.  Past {@link MAX_CHUNKS} the oldest messages
 * are dropped, and the first chunk says how many.
 */
export function chunkTranscript(messages: ChatMessage[]): string[] {
  const blocks = messages
    .map((message) => renderMessage(message, SUMMARIZER_LIMITS))
    .filter((block): block is string => block !== null)
    .map((block) => truncate(block, MAX_CHUNK_CHARS));
  const groups = groupBlocks(blocks);
  const kept = groups.slice(-MAX_CHUNKS);
  const omitted = groups
    .slice(0, groups.length - kept.length)
    .reduce((count, group) => count + group.length, 0);
  const chunks = kept.map((group) => group.join(BLOCK_SEPARATOR));
  if (omitted > 0) {
    chunks[0] = `[... ${omitted} earlier messages omitted ...]${BLOCK_SEPARATOR}${chunks[0]}`;
  }
  return chunks;
}

/** Who wrote a passage; a tool call with its result is the assistant's. */
export type PassageKind = 'user' | 'assistant' | 'tool';

/** Where one part (a text, a file or a tool call with its result) sits in its turn's text. */
export interface PassageSpan {
  start: number;
  end: number;
  kind: PassageKind;
  /** For a tool call: its name and the start of its input. */
  label?: string;
}

type PassageMeta = Pick<PassageSpan, 'kind' | 'label'>;

export const TOOL_LABEL_INPUT_CHARS = 80;

/** One user turn the summary replaced: its parts rendered and joined, with their spans. */
export interface TranscriptTurn {
  /** 1-based. */
  turn: number;
  text: string;
  passages: PassageSpan[];
}

const PASSAGE_SEPARATOR = '\n';

function toolLabel(part: ToolLikePart): string {
  const input = stringify(part.input).replace(/\s+/g, ' ').trim();
  const preview = input.length > TOOL_LABEL_INPUT_CHARS ? `${input.slice(0, TOOL_LABEL_INPUT_CHARS)}…` : input;
  return `${toolNameOf(part)} ${preview}`.trim();
}

function passageMeta(message: ChatMessage, part: Part): PassageMeta {
  if (message.role === 'user') {
    return { kind: 'user' };
  }
  const candidate = part as { type: string };
  return isToolPart(candidate) ? { kind: 'tool', label: toolLabel(candidate) } : { kind: 'assistant' };
}

function addPassage(turn: TranscriptTurn, text: string, meta: PassageMeta): void {
  const start = turn.passages.length === 0 ? 0 : turn.text.length + PASSAGE_SEPARATOR.length;
  turn.text = turn.passages.length === 0 ? text : `${turn.text}${PASSAGE_SEPARATOR}${text}`;
  turn.passages.push({ start, end: turn.text.length, ...meta });
}

/**
 * The user turns *compaction* replaced (everything before its boundary),
 * rendered like the summarizer's input but with each part kept up to
 * `MAX_SEARCH_PART_CHARS`; empty without a compaction.  Search matches the
 * parts one by one, so a hit in a long turn points at the tool result, and
 * reading pages through a turn's text, so a hit's offset is where to read.
 */
export function summarizedTurns(
  messages: ChatMessage[],
  compaction: CompactionView | null,
): TranscriptTurn[] {
  const index = boundaryIndex(messages, compaction);
  if (index === null) {
    return [];
  }
  const turns: TranscriptTurn[] = [];
  for (const message of messages.slice(0, index)) {
    if (message.role === 'user' || turns.length === 0) {
      turns.push({ turn: turns.length + 1, text: '', passages: [] });
    }
    const current = turns[turns.length - 1];
    const speaker = speakerOf(message);
    for (const part of message.parts) {
      const line = renderPart(part, SEARCH_LIMITS);
      if (line !== null && line.trim() !== '') {
        addPassage(current, `${speaker}: ${line}`, passageMeta(message, part));
      }
    }
  }
  return turns;
}

/** One summarizer prompt: the summary so far, then the next chunk. */
export function summarizerInput(chunk: string, previousSummary: string | null): string {
  if (!previousSummary) {
    return chunk;
  }
  return `Summary of the conversation before this point:\n${previousSummary.trim()}${BLOCK_SEPARATOR}${chunk}`;
}

/** System prompt for the summarizer: the fixed keep-list, then additions. */
export function buildSummaryPrompt(instructions: string, focus: string | null): string {
  const sections = [SUMMARY_PROMPT];
  if (instructions.trim()) {
    sections.push(`Additional rules for this assistant:\n${instructions.trim()}`);
  }
  const trimmedFocus = focus?.trim().slice(0, MAX_FOCUS_CHARS);
  if (trimmedFocus) {
    sections.push(
      `The user asked to keep the summary focused on the following; give it priority:\n${trimmedFocus}`,
    );
  }
  return sections.join('\n\n');
}

/**
 * The agent's `custom_outputs[key]` from a raw stream chunk, or `undefined`
 * when the chunk does not carry it.  The runtime sends `custom_outputs` on
 * the trailing `response.completed` event, top level and inside `response`.
 */
export function customOutputFromRaw(raw: unknown, key: string): unknown {
  if (typeof raw !== 'object' || raw === null) {
    return undefined;
  }
  const event = raw as {
    type?: unknown;
    custom_outputs?: Record<string, unknown>;
    response?: { custom_outputs?: Record<string, unknown> };
  };
  if (event.type !== 'response.completed') {
    return undefined;
  }
  return event.custom_outputs?.[key] ?? event.response?.custom_outputs?.[key];
}

/** The agent's `custom_outputs.context_window` from a raw stream chunk, or `null`. */
export function contextWindowFromRaw(raw: unknown): ContextWindowReport | null {
  const entry = customOutputFromRaw(raw, 'context_window');
  if (typeof entry !== 'object' || entry === null) {
    return null;
  }
  const { estimated_input_tokens: tokens, limit_exceeded: exceeded } = entry as {
    estimated_input_tokens?: unknown;
    limit_exceeded?: unknown;
  };
  return {
    estimatedInputTokens: typeof tokens === 'number' && Number.isFinite(tokens) ? tokens : null,
    limitExceeded: exceeded === true,
  };
}

/** Whether a turn's report calls for compacting the chat. */
export function shouldCompact(
  report: ContextWindowReport | null,
  triggerTokens: number,
): boolean {
  if (!report) {
    return false;
  }
  return report.limitExceeded || (report.estimatedInputTokens ?? 0) >= triggerTokens;
}

/**
 * The context meter's reading for a turn, or `null` without a report.
 * Compacting only while some turn is old enough to summarize
 * (*compactable*), so the meter does not wait for a summary that never comes.
 */
export function contextReading(
  report: ContextWindowReport | null,
  triggerTokens: number,
  compactable: boolean,
  now: Date = new Date(),
): ContextWindowReading | null {
  if (!report) {
    return null;
  }
  return {
    estimatedInputTokens: report.estimatedInputTokens,
    triggerTokens,
    compacting: compactable && shouldCompact(report, triggerTokens),
    measuredAt: now.toISOString(),
  };
}
