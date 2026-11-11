/**
 * Agent Bricks: the agent's plan as the plan panel shows it
 * (`spec.runtime.context.plan`).  Pure, so the `node:test` harness can
 * import it; only type imports.
 */
import type { ChatMessage } from '@chat-template/core';

export type MilestoneStatus = 'open' | 'done' | 'dropped';

export interface Milestone {
  title: string;
  status: MilestoneStatus;
  /** The result of a done milestone, or why it was dropped. */
  note: string;
}

export interface ChatPlan {
  goal: string;
  milestones: Milestone[];
}

// Mirrors the stored format of payloads/agent/runtime/plan.py.
const GOAL_PREFIX = '**Goal:** ';
const NOTE_SEPARATOR = ' — ';
const MILESTONE_LINE = /^(?:\d+\.|-) \[(?<mark>[ x>-])\] (?<text>.*)$/;
// `[>]` marked the step in progress in plans saved before milestones.
const STATUS_BY_MARK: Readonly<Record<string, MilestoneStatus>> = {
  ' ': 'open',
  '>': 'open',
  x: 'done',
  '-': 'dropped',
};
// Mirrors UPDATE_PLAN_TOOL_NAME and DELETE_PLAN_TOOL_NAME in plan.py.
const PLAN_TOOL_NAMES: ReadonlySet<string> = new Set(['_update_plan', '_delete_plan']);

function parseMilestone(line: string): Milestone | null {
  const groups = MILESTONE_LINE.exec(line)?.groups;
  if (!groups) {
    return null;
  }
  const text = groups.text;
  const at = text.indexOf(NOTE_SEPARATOR);
  return {
    title: at < 0 ? text : text.slice(0, at),
    status: STATUS_BY_MARK[groups.mark],
    note: at < 0 ? '' : text.slice(at + NOTE_SEPARATOR.length),
  };
}

/** The stored plan markdown read back, or `null` when it has no milestones. */
export function parsePlan(content: string): ChatPlan | null {
  const lines = content.split(/\r?\n/);
  const milestones = lines.map(parseMilestone).filter((m): m is Milestone => m !== null);
  if (milestones.length === 0) {
    return null;
  }
  const firstLine = lines[0] ?? '';
  const goal = firstLine.startsWith(GOAL_PREFIX) ? firstLine.slice(GOAL_PREFIX.length) : firstLine;
  return { goal, milestones };
}

/** "2 of 5 done"; dropped milestones count as neither. */
export function planProgress(plan: ChatPlan): string {
  const kept = plan.milestones.filter((m) => m.status !== 'dropped');
  const done = kept.filter((m) => m.status === 'done').length;
  return `${done} of ${kept.length} done`;
}

/** How many plan updates and deletes in *messages* have finished; a change means the stored plan changed. */
export function finishedPlanCalls(messages: readonly ChatMessage[]): number {
  return messages
    .flatMap((m) => m.parts)
    .filter(
      (part) =>
        part.type === 'dynamic-tool' &&
        PLAN_TOOL_NAMES.has(part.toolName) &&
        part.state === 'output-available',
    ).length;
}
