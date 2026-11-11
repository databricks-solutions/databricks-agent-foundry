"""The plan tools (``spec.runtime.context.plan``): ``_update_plan`` and ``_delete_plan``.

The plan is the agent's working state for one chat (see
:mod:`payloads.agent.runtime.plan` for the models and rendering).  It is
kept with the chat in the chat-history Lakebase: the agent reads it once
per request and saves every change through the chat UI server's plan route
(:data:`PLAN_ROUTE`), as the user.  The tool definitions are fixed, so they
stay in the cached prefix; the plan itself is recited in the request
context, after the history.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, ClassVar, Final, Generic, TypeVar

import httpx
import mlflow
from agents import FunctionTool
from pydantic import BaseModel, ConfigDict, ValidationError

from payloads.agent.contracts import AgentPlanRuntime
from payloads.agent.runtime.chat_server import ChatRouteClient, chat_route_client
from payloads.agent.runtime.memory import REMEMBER_TOOL_NAME
from payloads.agent.runtime.plan import (
    DELETE_PLAN_TOOL_NAME,
    MAX_MILESTONES,
    MAX_NOTE_CHARS,
    MAX_TITLE_CHARS,
    UPDATE_PLAN_TOOL_NAME,
    Plan,
    PlanState,
    PlanUpdate,
    PlanUpdateError,
    apply_update,
    archive_plan,
    parse_plan,
    render_plan,
    replaces_finished,
)
from payloads.agent.runtime.tool_args import invalid_arguments

logger = logging.getLogger(__name__)

PLAN_ROUTE: Final = "/api/chat/{chat_id}/plan"
PLAN_GUIDANCE_HEADING: Final = "## Planning"

_PLAN_GUIDANCE: Final = (
    f"- For work that takes many tool calls or several turns, set a goal and 3 to 7 milestones with "
    f"`{UPDATE_PLAN_TOOL_NAME}` before you start, without being asked, and say so. The goal is the end "
    'state the user wants; each milestone is a checkable result ("Root cause confirmed"), not work to '
    'do ("Check the logs"). Skip the plan for simple questions.',
    "- Decide routine choices yourself and say what you chose. Stop and ask only at a real decision "
    "point: the request is ambiguous in a way that changes the result, the options trade off in ways "
    "only the user can judge, or the next step is costly, slow or hard to undo. Then offer two to four "
    "concrete options, recommend one, and wait for the answer.",
    "- Update the plan when milestones are done or dropped, or the approach changes, not after every "
    "action; marking several at once is fine. Mark a milestone done only once its result is in and "
    "checked, and give that result in one line. Your final answer is not a milestone: the plan is "
    "finished before you answer. Before your final answer, make sure the plan matches what was done.",
    "- The plan tracks progress on the current goal. Once nothing in it is open it is no longer shown "
    "in full; the finished goals of this chat are listed one line each.",
)
_MEMORY_HANDOFF: Final = (
    f"- Save anything that still matters after the goal is done, in a later chat, with `{REMEMBER_TOOL_NAME}`."
)
_UPDATE_PLAN_DESCRIPTION: Final = (
    "Start or update this chat's plan: a goal and numbered outcome milestones. Send only what changes: "
    "a goal to start a plan or change its aim, milestones to add, and milestones (by number) that are "
    "done, with their result, or dropped, with the reason. The open plan is shown to you before every "
    "model call."
)
_DELETE_PLAN_DESCRIPTION: Final = "Delete this chat's plan, when the user asks or the plan no longer applies."


@dataclass(frozen=True)
class PlanBinding:
    """One request's view of the plan: this chat's plan route and the deploy's settings.

    ``loaded`` is false when the stored plan could not be read; the tools
    then refuse, so a write never replaces a plan this request did not see.
    """

    client: ChatRouteClient
    settings: AgentPlanRuntime
    loaded: bool = True


class _NoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _StoredPlan(BaseModel):
    """The plan route's body: the open plan (``None`` without one) and the finished plans."""

    model_config = ConfigDict(extra="ignore")

    content: str | None = None
    history: str = ""


def _plan_summary(plan: Plan) -> str:
    done = sum(milestone.status == "done" for milestone in plan.milestones)
    open_titles = [f"{n}. {m.title}" for n, m in enumerate(plan.milestones, 1) if m.status == "open"]
    if not open_titles:
        return "Plan saved; no milestone is open. It is listed with the finished goals of this chat."
    return f"Plan saved: {done} of {len(plan.milestones)} done. Open: {'; '.join(open_titles)}"


_ArgsT = TypeVar("_ArgsT", bound=BaseModel)
_UNAVAILABLE: Final = "Error: the plan is unavailable right now; continue without it."


class _PlanTool(Generic[_ArgsT]):
    """Parses the arguments, saves through the plan route, keeps the recited plan in step."""

    args_model: ClassVar[type[BaseModel]]

    def __init__(self, binding: PlanBinding, state: PlanState) -> None:
        self._binding = binding
        self._state = state

    async def __call__(self, _ctx: Any, args_json: str) -> str:
        try:
            args = self.args_model.model_validate_json(args_json or "{}")
        except ValidationError as exc:
            return invalid_arguments(exc)
        if not self._binding.loaded:
            return _UNAVAILABLE
        try:
            return await self._run(args)  # type: ignore[arg-type]
        except httpx.HTTPError as exc:
            logger.warning("Plan tool %s failed: %s", type(self).__name__, exc)
            return _UNAVAILABLE

    async def _save(self, content: str, history: str) -> None:
        """Store the plan and its history together, then update the request's copy."""
        await self._binding.client.request("PUT", body={"content": content, "history": history})
        self._state.saved(content)
        self._state.archived(history)

    async def _run(self, args: _ArgsT) -> str:
        raise NotImplementedError


class UpdatePlan(_PlanTool[PlanUpdate]):
    args_model = PlanUpdate

    async def _run(self, args: PlanUpdate) -> str:
        stored = self._state.content
        current = parse_plan(stored) if stored else None
        try:
            plan = apply_update(current, args)
        except PlanUpdateError as exc:
            return f"Error: {exc}."
        content = render_plan(plan)
        limit = self._binding.settings.max_chars
        if len(content) > limit:
            return f"Error: the plan is longer than {limit} characters; shorten the milestones or results."
        history = self._state.history
        if current is not None and replaces_finished(current, args):
            history = archive_plan(history, stored, limit)
        await self._save(content, history)
        return _plan_summary(plan)


class DeletePlan(_PlanTool[_NoArgs]):
    args_model = _NoArgs

    async def _run(self, args: _NoArgs) -> str:
        if not self._state.content:
            return "No plan in this chat."
        await self._save("", self._state.history)
        return "Plan deleted."


def _milestone_changes(note: str, help_text: str) -> dict[str, Any]:
    return {
        "type": "array",
        "description": help_text,
        "items": {
            "type": "object",
            "properties": {
                "milestone": {"type": "integer", "minimum": 1, "description": "The number shown in the plan."},
                note: {"type": "string", "maxLength": MAX_NOTE_CHARS},
            },
            "required": ["milestone", note],
            "additionalProperties": False,
        },
    }


_PLAN_PARAMS: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "goal": {
            "type": "string",
            "description": "The objective of the whole task, in one sentence. Required to start a plan; "
            "a new goal after a finished plan starts a new one.",
        },
        "add": {
            "type": "array",
            "maxItems": MAX_MILESTONES,
            "items": {"type": "string", "maxLength": MAX_TITLE_CHARS},
            "description": 'Milestones to append: checkable results ("Root cause confirmed"), '
            "not work to do or the final answer.",
        },
        "done": _milestone_changes("result", "Milestones reached, each with its result in one line."),
        "drop": _milestone_changes("reason", "Milestones that no longer apply, each with why."),
    },
    "additionalProperties": False,
}
_NO_PARAMS: Final[dict[str, Any]] = {"type": "object", "properties": {}, "additionalProperties": False}


def plan_function_tools(binding: PlanBinding, state: PlanState) -> tuple[FunctionTool, FunctionTool]:
    """``_update_plan`` and ``_delete_plan``; their definitions are fixed, writes update *state*."""
    return (
        FunctionTool(
            name=UPDATE_PLAN_TOOL_NAME,
            description=_UPDATE_PLAN_DESCRIPTION,
            params_json_schema=_PLAN_PARAMS,
            on_invoke_tool=UpdatePlan(binding, state),
            strict_json_schema=False,
        ),
        FunctionTool(
            name=DELETE_PLAN_TOOL_NAME,
            description=_DELETE_PLAN_DESCRIPTION,
            params_json_schema=_NO_PARAMS,
            on_invoke_tool=DeletePlan(binding, state),
            strict_json_schema=False,
        ),
    )


def render_plan_guidance(*, memory: bool) -> str:
    """How to plan, for the cached prefix; with *memory* on, where lasting facts go."""
    lines = [PLAN_GUIDANCE_HEADING, *_PLAN_GUIDANCE]
    if memory:
        lines.append(_MEMORY_HANDOFF)
    return "\n".join(lines)


@dataclass(frozen=True)
class AgentPlan:
    """This request's plan tools, planning guidance and the chat's plan state."""

    tools: tuple[FunctionTool, ...]
    guidance: str
    state: PlanState


async def _load_plan(client: ChatRouteClient) -> PlanState | None:
    """The chat's stored plan; ``None`` (logged) when it cannot be read."""
    with mlflow.start_span(name="plan.load", span_type="TASK"):
        try:
            stored = _StoredPlan.model_validate_json(await client.request("GET"))
        except (httpx.HTTPError, ValidationError) as exc:
            logger.warning("Plan unavailable for this request: %s", exc)
            return None
    return PlanState(stored.content or "", stored.history)


async def prepare_plan(
    settings: AgentPlanRuntime,
    conversation_id: str,
    forwarded_headers: Mapping[str, str],
    *,
    memory: bool,
) -> AgentPlan | None:
    """This chat's plan tools and stored plan; ``None`` without a usable conversation id."""
    client = chat_route_client(PLAN_ROUTE, conversation_id, forwarded_headers)
    if client is None:
        return None
    state = await _load_plan(client)
    binding = PlanBinding(client, settings, loaded=state is not None)
    state = state or PlanState()
    return AgentPlan(
        tools=plan_function_tools(binding, state),
        guidance=render_plan_guidance(memory=memory),
        state=state,
    )
