/**
 * Replace ``echarts-chart`` fences in earlier assistant turns with a short
 * placeholder, for the model request only.
 *
 * A chart fence carries its full data inline, so every later turn would
 * send that data back to the model as input tokens.  The persisted chat is
 * untouched — the UI keeps rendering the chart — and the model still learns
 * that a chart was shown, with its type, title and row count.
 *
 * A part holding only a fence, after a ``_chart`` tool part, is the chart
 * that tool filed; the model already has the call and its summary, so the
 * part is dropped.  A placeholder there reads as the way to end a chart
 * turn, and the model starts writing it into its answers.  A
 * ``markdown-report`` fence after a ``_report`` tool part is dropped the
 * same way: the call's arguments already hold the whole report.  A report
 * the model wrote itself stays, so it can revise it on a later turn.
 *
 * Pure transform that returns new message/part objects only where a fence
 * was replaced; unit-tested out of tree at ``payloads/agent/ui/tests/ts/``.
 */
import type { ChatMessage } from '@chat-template/core';

const CHART_FENCE = /```echarts-chart\n([\s\S]*?)\n```/g;
const LONE_CHART_FENCE = /^\s*```echarts-chart\n[\s\S]*\n```\s*$/;
// Report fences are at least four backticks long (see report_tool.py).
const LONE_REPORT_FENCE = /^\s*(`{4,})markdown-report[^\n]*\n[\s\S]*\n\1\s*$/;
const MAX_TITLE_CHARS = 80;

interface ToolFiledFence {
  toolName: string;
  loneFence: RegExp;
}

const TOOL_FILED_FENCES: readonly ToolFiledFence[] = [
  { toolName: '_chart', loneFence: LONE_CHART_FENCE },
  { toolName: '_report', loneFence: LONE_REPORT_FENCE },
];

interface TextPart {
  type: 'text';
  text: string;
}

function isTextPart(part: unknown): part is TextPart {
  return (
    typeof part === 'object' &&
    part !== null &&
    (part as { type?: unknown }).type === 'text' &&
    typeof (part as { text?: unknown }).text === 'string'
  );
}

function parseSpec(body: string): Record<string, unknown> | null {
  try {
    const parsed: unknown = JSON.parse(body);
    return typeof parsed === 'object' && parsed !== null ? (parsed as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}

function describeChart(spec: Record<string, unknown>): string {
  const details: string[] = [];
  if (typeof spec.type === 'string') {
    details.push(spec.type);
  }
  if (typeof spec.title === 'string' && spec.title) {
    details.push(JSON.stringify(spec.title.slice(0, MAX_TITLE_CHARS)));
  }
  if (Array.isArray(spec.data)) {
    details.push(`${spec.data.length} rows`);
  }
  return details.join(', ');
}

/** The placeholder text for one fence body. */
export function chartPlaceholder(body: string): string {
  const spec = parseSpec(body);
  const description = spec ? describeChart(spec) : '';
  const shown = description ? `An interactive chart was shown here (${description}).` : 'An interactive chart was shown here.';
  return `[${shown} Its data is left out of this history.]`;
}

function replaceFences(text: string): string {
  return text.replace(CHART_FENCE, (_fence, body: string) => chartPlaceholder(body));
}

/** The tool a ``tool-<name>`` or ``dynamic-tool`` part calls, else ``null``. */
function toolNameOf(part: unknown): string | null {
  if (typeof part !== 'object' || part === null) {
    return null;
  }
  const { type, toolName } = part as { type?: unknown; toolName?: unknown };
  if (type === 'dynamic-tool') {
    return typeof toolName === 'string' ? toolName : null;
  }
  return typeof type === 'string' && type.startsWith('tool-') ? type.slice('tool-'.length) : null;
}

function isToolFiledFence(part: unknown, calledTools: ReadonlySet<string>): boolean {
  return (
    isTextPart(part) &&
    TOOL_FILED_FENCES.some(({ toolName, loneFence }) => calledTools.has(toolName) && loneFence.test(part.text))
  );
}

function replaceInPart(part: unknown): unknown {
  if (!isTextPart(part)) {
    return part;
  }
  const text = replaceFences(part.text);
  return text === part.text ? part : { ...part, text };
}

/** The parts without tool-filed fences and with other chart fences replaced, or ``null`` when nothing changed. */
function omitInParts(parts: unknown[]): unknown[] | null {
  const next: unknown[] = [];
  const calledTools = new Set<string>();
  let changed = false;
  for (const part of parts) {
    const toolName = toolNameOf(part);
    if (toolName) {
      calledTools.add(toolName);
    }
    if (isToolFiledFence(part, calledTools)) {
      changed = true;
      continue;
    }
    const replaced = replaceInPart(part);
    changed ||= replaced !== part;
    next.push(replaced);
  }
  return changed ? next : null;
}

export function omitChartData(messages: ChatMessage[]): ChatMessage[] {
  return messages.map((message) => {
    const { role, parts } = message as { role?: unknown; parts?: unknown };
    if (role !== 'assistant' || !Array.isArray(parts)) {
      return message;
    }
    const nextParts = omitInParts(parts);
    return nextParts ? ({ ...message, parts: nextParts } as ChatMessage) : message;
  });
}
