"""Tests for the plan tools, their storage through the chat UI server's plan route, and the plan guidance."""

from __future__ import annotations

import asyncio
import functools
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
from agents import FunctionTool

from payloads.agent.contracts import AgentPlanRuntime
from payloads.agent.runtime.chat_server import ChatRouteClient
from payloads.agent.runtime.memory import REMEMBER_TOOL_NAME
from payloads.agent.runtime.plan import (
    DELETE_PLAN_TOOL_NAME,
    FINISHED_HEADING,
    PLAN_HEADING,
    UPDATE_PLAN_TOOL_NAME,
    PlanState,
)
from payloads.agent.runtime.plan_tools import (
    PLAN_GUIDANCE_HEADING,
    PLAN_ROUTE,
    AgentPlan,
    PlanBinding,
    plan_function_tools,
    prepare_plan,
    render_plan_guidance,
)
from payloads.agent.tests.unit.runtime.conftest import CHAT

USER = {"x-forwarded-user": "alice-id", "x-forwarded-email": "alice@example.com"}
CLIENT = ChatRouteClient(f"http://localhost:3000{PLAN_ROUTE.format(chat_id=CHAT)}", USER)
UI_SERVER = Path(__file__).parents[3] / "ui/overrides/server/src"
_OPEN = "**Goal:** Q3\n1. [ ] Inventory"


@dataclass
class FakePlanRoute:
    """The chat UI server's plan route for one chat; ``status`` other than 200 fails every call."""

    content: str = ""
    history: str = ""
    status: int = 200
    requests: list[httpx.Request] = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.url.path == PLAN_ROUTE.format(chat_id=CHAT)
        if self.status != 200:
            return httpx.Response(self.status, json={"error": "unavailable"})
        if request.method == "GET":
            return httpx.Response(200, json={"content": self.content or None, "history": self.history})
        stored = json.loads(request.content)
        self.content, self.history = stored["content"], stored["history"]
        return httpx.Response(204)

    def methods(self) -> list[str]:
        return [request.method for request in self.requests]


def _patched(route: FakePlanRoute) -> Any:
    client = functools.partial(httpx.AsyncClient, transport=httpx.MockTransport(route))
    return patch("payloads.agent.runtime.chat_server.Latin1HeaderClient", client)


async def _invoke(tool: FunctionTool, **args: Any) -> str:
    return str(await tool.on_invoke_tool(MagicMock(), json.dumps(args)))


@dataclass
class _Chat:
    """One request's plan tools over *route*."""

    route: FakePlanRoute
    state: PlanState
    binding: PlanBinding

    def call(self, tool: str, **args: Any) -> str:
        tools = {t.name: t for t in plan_function_tools(self.binding, self.state)}
        with _patched(self.route):
            return asyncio.run(_invoke(tools[tool], **args))


def _chat(
    route: FakePlanRoute | None = None, *, max_chars: int = 8_000, loaded: bool = True, state: PlanState | None = None
) -> _Chat:
    binding = PlanBinding(CLIENT, AgentPlanRuntime(enabled=True, max_chars=max_chars), loaded=loaded)
    return _Chat(route or FakePlanRoute(), state or PlanState(), binding)


def _done(*numbers: int) -> list[dict[str, Any]]:
    return [{"milestone": number, "result": "ok"} for number in numbers]


class TestUpdatePlan:
    def test_creates_then_patches_the_chat_plan_as_the_user(self) -> None:
        chat = _chat()
        first = chat.call(UPDATE_PLAN_TOOL_NAME, goal="Q3", add=["Inventory", "Costs"])
        assert first == "Plan saved: 0 of 2 done. Open: 1. Inventory; 2. Costs"
        chat.call(UPDATE_PLAN_TOOL_NAME, done=[{"milestone": 1, "result": "412 tables"}])
        assert chat.route.content == "**Goal:** Q3\n1. [x] Inventory — 412 tables\n2. [ ] Costs"
        assert chat.route.methods() == ["PUT", "PUT"]
        assert all(request.headers["x-forwarded-user"] == "alice-id" for request in chat.route.requests)

    def test_a_finished_plan_says_where_it_went(self) -> None:
        chat = _chat()
        chat.call(UPDATE_PLAN_TOOL_NAME, goal="Q3", add=["Inventory"])
        result = chat.call(UPDATE_PLAN_TOOL_NAME, done=_done(1))
        assert result.startswith("Plan saved; no milestone is open.") and "finished goals" in result

    def test_rejections_say_why_and_save_nothing(self) -> None:
        chat = _chat(max_chars=20)
        assert "there is no plan yet" in chat.call(UPDATE_PLAN_TOOL_NAME, add=["Inventory"])
        assert "invalid arguments" in chat.call(UPDATE_PLAN_TOOL_NAME, steps=[])
        assert "longer than 20" in chat.call(UPDATE_PLAN_TOOL_NAME, goal="Q3", add=["Inventory"])
        assert chat.route.requests == []

    def test_writes_update_the_recited_plan(self) -> None:
        chat = _chat()
        chat.call(UPDATE_PLAN_TOOL_NAME, goal="Q3", add=["Inventory"])
        assert "1. [ ] Inventory" in chat.state.recite()
        chat.call(DELETE_PLAN_TOOL_NAME)
        assert chat.state.recite() == ""

    def test_a_rejected_update_keeps_the_recited_plan(self) -> None:
        chat = _chat()
        chat.call(UPDATE_PLAN_TOOL_NAME, goal="Q3", add=["Inventory"])
        recited = chat.state.recite()
        chat.call(UPDATE_PLAN_TOOL_NAME, done=_done(5))
        assert chat.state.recite().startswith(recited)

    def test_a_new_goal_archives_the_finished_plan_in_the_same_save(self) -> None:
        chat = _chat()
        chat.call(UPDATE_PLAN_TOOL_NAME, goal="Q3", add=["Inventory"])
        chat.call(UPDATE_PLAN_TOOL_NAME, done=_done(1))
        chat.call(UPDATE_PLAN_TOOL_NAME, goal="Q4", add=["Costs"])
        assert chat.route.history == "**Goal:** Q3\n1. [x] Inventory — ok"
        assert chat.route.content == "**Goal:** Q4\n1. [ ] Costs"
        assert chat.route.methods() == ["PUT", "PUT", "PUT"]
        assert chat.state.recite().endswith(f"{FINISHED_HEADING}\n- Q3 (1 done)")

    def test_an_open_plan_or_a_reopened_one_is_not_archived(self) -> None:
        chat = _chat()
        chat.call(UPDATE_PLAN_TOOL_NAME, goal="Q3", add=["Inventory"])
        chat.call(UPDATE_PLAN_TOOL_NAME, goal="Q4")
        chat.call(UPDATE_PLAN_TOOL_NAME, done=_done(1))
        chat.call(UPDATE_PLAN_TOOL_NAME, add=["Costs"])
        assert chat.route.history == ""
        assert chat.route.content == "**Goal:** Q4\n1. [x] Inventory — ok\n2. [ ] Costs"

    def test_patches_a_plan_saved_as_a_step_checklist(self) -> None:
        chat = _chat(state=PlanState("**Goal:** Q3\n- [x] Step 1\n- [>] Step 2"))
        chat.call(UPDATE_PLAN_TOOL_NAME, done=_done(2))
        assert chat.route.content == "**Goal:** Q3\n1. [x] Step 1\n2. [x] Step 2 — ok"

    def test_a_failed_save_is_a_tool_message_and_keeps_the_recited_plan(self) -> None:
        chat = _chat(FakePlanRoute(status=503), state=PlanState(_OPEN))
        recited = chat.state.recite()
        assert "plan is unavailable" in chat.call(UPDATE_PLAN_TOOL_NAME, done=_done(1))
        assert chat.state.recite() == recited

    def test_refuses_when_the_plan_could_not_be_loaded(self) -> None:
        chat = _chat(loaded=False)
        assert "plan is unavailable" in chat.call(UPDATE_PLAN_TOOL_NAME, goal="Q3", add=["a"])
        assert "plan is unavailable" in chat.call(DELETE_PLAN_TOOL_NAME)
        assert chat.route.requests == []


class TestDeletePlan:
    def test_says_when_there_is_no_plan(self) -> None:
        chat = _chat()
        assert chat.call(DELETE_PLAN_TOOL_NAME) == "No plan in this chat."
        assert chat.route.requests == []

    def test_clears_the_plan_and_keeps_the_finished_goals(self) -> None:
        history = "**Goal:** Q2\n1. [x] a — ok"
        chat = _chat(FakePlanRoute(_OPEN, history), state=PlanState(_OPEN, history))
        assert chat.call(DELETE_PLAN_TOOL_NAME) == "Plan deleted."
        assert (chat.route.content, chat.route.history) == ("", history)


class TestGuidance:
    def test_hands_lasting_facts_to_memory_only_when_it_is_on(self) -> None:
        assert REMEMBER_TOOL_NAME in render_plan_guidance(memory=True)
        without = render_plan_guidance(memory=False)
        assert without.startswith(PLAN_GUIDANCE_HEADING) and REMEMBER_TOOL_NAME not in without

    def test_milestones_are_results_not_work(self) -> None:
        guidance = render_plan_guidance(memory=False)
        assert "end state the user wants" in guidance and "checkable result" in guidance
        chat = _chat()
        update, _ = plan_function_tools(chat.binding, chat.state)
        assert "checkable results" in update.params_json_schema["properties"]["add"]["description"]

    def test_asks_only_at_decision_points(self) -> None:
        guidance = render_plan_guidance(memory=False)
        assert "Decide routine choices yourself" in guidance and "recommend one" in guidance


def _prepare(route: FakePlanRoute, conversation_id: str = CHAT, headers: dict[str, str] = USER) -> AgentPlan | None:
    with _patched(route):
        return asyncio.run(prepare_plan(AgentPlanRuntime(enabled=True), conversation_id, headers, memory=False))


class TestPreparePlan:
    def test_recites_the_stored_plan_and_finished_goals(self) -> None:
        route = FakePlanRoute(_OPEN, "**Goal:** Q2\n1. [x] a — ok")
        plan = _prepare(route, headers={**USER, "x-forwarded-access-token": "secret", "cookie": "c"})
        assert plan is not None
        recited = plan.state.recite()
        assert recited.startswith(f"{PLAN_HEADING}\n{_OPEN}") and "- Q2 (1 done)" in recited
        assert {tool.name for tool in plan.tools} == {UPDATE_PLAN_TOOL_NAME, DELETE_PLAN_TOOL_NAME}
        assert UPDATE_PLAN_TOOL_NAME in plan.guidance
        (request,) = route.requests
        assert request.method == "GET" and request.headers["x-forwarded-user"] == "alice-id"
        assert "x-forwarded-access-token" not in request.headers and "cookie" not in request.headers

    def test_a_failed_load_keeps_the_tools_and_never_overwrites(self) -> None:
        route = FakePlanRoute(status=503)
        plan = _prepare(route)
        assert plan is not None and plan.state.recite() == ""
        assert len(plan.tools) == 2
        update = next(tool for tool in plan.tools if tool.name == UPDATE_PLAN_TOOL_NAME)
        with _patched(route):
            reply = asyncio.run(_invoke(update, goal="Q3", add=["a"]))
        assert "plan is unavailable" in reply
        assert route.methods() == ["GET"]

    def test_off_without_a_plain_conversation_id(self) -> None:
        route = FakePlanRoute()
        assert _prepare(route, conversation_id="a/b") is None
        assert route.requests == []

    def test_ui_server_serves_the_route(self) -> None:
        mount, _, path = PLAN_ROUTE.partition("/{chat_id}")
        assert f"app.use('{mount}', chatPlanRouter)" in (UI_SERVER / "index.ts").read_text(encoding="utf-8")
        routes = (UI_SERVER / "routes/chat-plan.ts").read_text(encoding="utf-8")
        assert f"chatPlanRouter.get('/:id{path}'" in routes and f"chatPlanRouter.put('/:id{path}'" in routes
