"""The per-chat plan (``spec.runtime.context.plan``).

A goal and a few outcome milestones the agent keeps for long, multi-step
work, like the feature list of Anthropic's long-running agent harness:
milestones are results to reach, not single actions, several can be worked
on or finished in one go, and each finished one keeps a one-line result.
Updates are patches (mark done, drop, add, change the goal), so the model
never resends the whole plan.  The open plan is recited at the end of the
request before every model call, with a reminder when it has not been
updated for a while.  It is stored with the chat in the chat-history
Lakebase, so it survives compaction and is deleted with the chat.  A
finished plan that a new goal replaces moves to the chat's plan history;
the recited context lists the finished goals, one line each.

Pure models, rendering and state only; the tools live in
:mod:`payloads.agent.runtime.plan_tools`.
"""

from __future__ import annotations

import re
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

UPDATE_PLAN_TOOL_NAME: Final = "_update_plan"
DELETE_PLAN_TOOL_NAME: Final = "_delete_plan"
PLAN_HEADING: Final = "## Current plan"
FINISHED_HEADING: Final = "## Finished goals in this chat"
MAX_FINISHED_SHOWN: Final = 5
MAX_MILESTONES: Final = 10
MAX_TITLE_CHARS: Final = 200
MAX_NOTE_CHARS: Final = 300
# Model calls without a plan update before the recited plan carries a reminder.
NUDGE_AFTER_CALLS: Final = 8

MilestoneStatus = Literal["open", "done", "dropped"]

_MARKS: Final[dict[MilestoneStatus, str]] = {"open": " ", "done": "x", "dropped": "-"}
_STATUS_BY_MARK: Final[dict[str, MilestoneStatus]] = {
    **{mark: status for status, mark in _MARKS.items()},
    # Plans saved before milestones marked the step in progress with ``[>]``.
    ">": "open",
}
_GOAL_PREFIX: Final = "**Goal:** "
_NOTE_SEPARATOR: Final = " — "
_MILESTONE_LINE: Final = re.compile(r"^(?:\d+\.|-) \[(?P<mark>[ x>-])\] (?P<text>.*)$")


class Milestone(BaseModel):
    title: str
    status: MilestoneStatus = "open"
    # The result of a done milestone, or why it was dropped.
    note: str = ""


class Plan(BaseModel):
    goal: str
    milestones: list[Milestone]

    @property
    def is_open(self) -> bool:
        return any(milestone.status == "open" for milestone in self.milestones)


class _UpdateArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MilestoneDone(_UpdateArgs):
    milestone: int = Field(ge=1)
    result: str = Field(min_length=1, max_length=MAX_NOTE_CHARS)


class MilestoneDropped(_UpdateArgs):
    milestone: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=MAX_NOTE_CHARS)


_MilestoneTitle = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_TITLE_CHARS)]


class PlanUpdate(_UpdateArgs):
    goal: str | None = Field(default=None, min_length=1, max_length=500)
    add: list[_MilestoneTitle] = Field(default_factory=list, max_length=MAX_MILESTONES)
    done: list[MilestoneDone] = Field(default_factory=list)
    drop: list[MilestoneDropped] = Field(default_factory=list)

    @model_validator(mode="after")
    def _changes_something(self) -> PlanUpdate:
        if self.goal is None and not (self.add or self.done or self.drop):
            raise ValueError("send a goal, or milestones to add, mark done or drop")
        return self


class PlanUpdateError(ValueError):
    """An update that does not fit the stored plan; the message is shown to the model."""


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _title(text: str) -> str:
    """One line without the result separator, so the stored line parses back."""
    return _one_line(text).replace(_NOTE_SEPARATOR.strip(), "-")


def replaces_finished(current: Plan, update: PlanUpdate) -> bool:
    """Whether *update* starts another goal after *current* was finished."""
    return update.goal is not None and not current.is_open and _one_line(update.goal) != current.goal


def _set_status(plan: Plan, number: int, status: MilestoneStatus, note: str) -> None:
    if not 1 <= number <= len(plan.milestones):
        raise PlanUpdateError(f"there is no milestone {number}; the plan has {len(plan.milestones)}")
    milestone = plan.milestones[number - 1]
    milestone.status = status
    milestone.note = _one_line(note)


def apply_update(current: Plan | None, update: PlanUpdate) -> Plan:
    """*current* with *update* applied; a goal after a finished plan starts a new one.

    Milestone numbers refer to the plan as shown, before *update* adds any.
    """
    if current is None or replaces_finished(current, update):
        if update.goal is None:
            raise PlanUpdateError("there is no plan yet; start one with a goal and milestones to add")
        plan = Plan(goal=_one_line(update.goal), milestones=[])
    else:
        plan = current.model_copy(deep=True)
        if update.goal is not None:
            plan.goal = _one_line(update.goal)
    for done in update.done:
        _set_status(plan, done.milestone, "done", done.result)
    for dropped in update.drop:
        _set_status(plan, dropped.milestone, "dropped", dropped.reason)
    plan.milestones.extend(Milestone(title=_title(title)) for title in update.add)
    if not plan.milestones:
        raise PlanUpdateError("a plan needs at least one milestone")
    if len(plan.milestones) > MAX_MILESTONES:
        raise PlanUpdateError(f"a plan has at most {MAX_MILESTONES} milestones; drop or merge some")
    return plan


def _milestone_line(number: int, milestone: Milestone) -> str:
    line = f"{number}. [{_MARKS[milestone.status]}] {milestone.title}"
    return f"{line}{_NOTE_SEPARATOR}{milestone.note}" if milestone.note else line


def render_plan(plan: Plan) -> str:
    """The stored markdown: the goal, then one numbered line per milestone."""
    lines = [f"{_GOAL_PREFIX}{plan.goal}"]
    lines.extend(_milestone_line(number, milestone) for number, milestone in enumerate(plan.milestones, 1))
    return "\n".join(lines)


def _parse_milestone(line: str) -> Milestone | None:
    match = _MILESTONE_LINE.match(line)
    if match is None:
        return None
    title, _, note = match["text"].partition(_NOTE_SEPARATOR)
    return Milestone(title=title, status=_STATUS_BY_MARK[match["mark"]], note=note)


def parse_plan(content: str) -> Plan:
    """A stored plan read back; also reads plans saved as step checklists."""
    milestones = [milestone for line in content.splitlines() if (milestone := _parse_milestone(line))]
    return Plan(goal=plan_goal(content), milestones=milestones)


def plan_is_open(content: str) -> bool:
    """Whether a stored plan still has an open milestone."""
    return parse_plan(content).is_open


def plan_goal(content: str) -> str:
    """The goal of a stored plan, without its prefix."""
    first_line = next(iter(content.splitlines()), "")
    return first_line.removeprefix(_GOAL_PREFIX)


def _history_plans(history: str) -> list[str]:
    return [plan for plan in history.split("\n\n") if plan.strip()]


def archive_plan(history: str, plan: str, max_chars: int) -> str:
    """*history* with *plan* appended, dropping the oldest plans beyond *max_chars*."""
    plans = [*_history_plans(history), plan]
    while len(plans) > 1 and len("\n\n".join(plans)) > max_chars:
        plans.pop(0)
    return "\n\n".join(plans)


def _outcome(plan: Plan) -> str:
    """How a finished plan ended, such as ``5 done, 1 dropped``."""
    counts = {status: sum(m.status == status for m in plan.milestones) for status in ("done", "dropped")}
    return ", ".join(f"{count} {status}" for status, count in counts.items() if count)


def render_finished_goals(history: str, current: str) -> str:
    """The request-context block listing the chat's last finished goals; empty when there are none.

    The plans in *history*, then *current* once nothing in it is open.
    """
    plans = [parse_plan(content) for content in _history_plans(history)]
    if current and not plan_is_open(current):
        plans.append(parse_plan(current))
    if not plans:
        return ""
    lines = [FINISHED_HEADING]
    lines.extend(f"- {plan.goal} ({_outcome(plan)})" for plan in plans[-MAX_FINISHED_SHOWN:])
    return "\n".join(lines)


def render_plan_block(content: str, calls_since_update: int = 0) -> str:
    """The request-context block for an open plan; empty once no milestone is open."""
    if not plan_is_open(content):
        return ""
    lines = [
        PLAN_HEADING,
        content,
        f"Update it with `{UPDATE_PLAN_TOOL_NAME}` when milestones are done or dropped, or the approach "
        "changes; [x] done, [-] dropped, [ ] open.",
    ]
    if calls_since_update >= NUDGE_AFTER_CALLS:
        lines.append(
            f"Not updated for {calls_since_update} model calls: mark finished milestones done "
            "or drop the ones that no longer apply."
        )
    return "\n".join(lines)


class PlanState:
    """This request's copy of the stored plan and its history, recited before every model call.

    The plan tools call :meth:`saved` and :meth:`archived` after each
    write, so reciting never reads the store.
    """

    def __init__(self, content: str = "", history: str = "") -> None:
        self._content = content
        self._history = history
        self._calls_since_update = 0

    @property
    def content(self) -> str:
        return self._content

    @property
    def history(self) -> str:
        return self._history

    def saved(self, content: str) -> None:
        self._content = content
        self._calls_since_update = 0

    def archived(self, history: str) -> None:
        self._history = history

    def recite(self) -> str:
        """The plan and finished-goals blocks for the next model call, counting the call."""
        blocks = (
            render_plan_block(self._content, self._calls_since_update),
            render_finished_goals(self._history, self._content),
        )
        self._calls_since_update += 1
        return "\n\n".join(block for block in blocks if block)
