"""Tests for the long-term memory tools, their calls to the chat UI server's memory routes, and the topic index."""

from __future__ import annotations

import asyncio
import functools
import json
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
from agents import FunctionTool

from payloads.agent.contracts import AgentMemoryRuntime
from payloads.agent.runtime.chat_server import user_route_client
from payloads.agent.runtime.memory import (
    FORGET_TOOL_NAME,
    LIST_MEMORIES_TOOL_NAME,
    MEMORY_ROUTE,
    RECALL_TOOL_NAME,
    REMEMBER_TOOL_NAME,
    MemoryTopic,
    forget_slug,
    memory_function_tools,
    prepare_memory,
    render_memory_guidance,
    render_memory_index,
    topic_slug,
)
from payloads.agent.tests.unit.runtime.conftest import ALICE

USER = {"x-forwarded-user": ALICE, "x-forwarded-email": "alice@example.com", "x-other": "dropped"}


@dataclass
class FakeMemoryRoute:
    """The chat UI server's memory routes for one user; ``status`` other than 200 fails every call."""

    facts: dict[str, list[str]] = field(default_factory=dict)
    max_chars: int = 8_000
    status: int = 200
    requests: list[httpx.Request] = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={"detail": "unavailable"})
        action = request.url.path.removeprefix(f"{MEMORY_ROUTE}/")
        body = json.loads(request.content) if request.content else {}
        if action == "topics":
            topics = [{"topic": topic, "facts": len(facts)} for topic, facts in sorted(self.facts.items())]
            return httpx.Response(200, json={"topics": topics})
        if action.startswith("topics/"):
            topic = action.removeprefix("topics/")
            return httpx.Response(200, json={"topic": topic, "facts": self.facts.get(topic, [])})
        if action == "search":
            hits = [
                {"topic": topic, "fact": fact}
                for topic, facts in self.facts.items()
                for fact in facts
                if body["query"].lower() in fact.lower()
            ]
            return httpx.Response(200, json={"hits": hits})
        return self._remember(body) if action == "remember" else self._forget(body)

    def _remember(self, body: dict[str, str]) -> httpx.Response:
        fact = " ".join(body["fact"].split())
        if len(fact) > self.max_chars:
            return httpx.Response(400, json={"detail": f"The fact is longer than {self.max_chars} characters."})
        facts = self.facts.setdefault(body["topic"], [])
        if fact.casefold() in (stored.casefold() for stored in facts):
            return httpx.Response(200, json={"status": "exists"})
        if len("\n".join([*facts, fact])) > self.max_chars:
            return httpx.Response(409, json={"detail": f"/{body['topic']}.md is full ({self.max_chars} characters)."})
        facts.append(fact)
        return httpx.Response(201, json={"status": "created"})

    def _forget(self, body: dict[str, str]) -> httpx.Response:
        facts = self.facts.get(body["topic"], [])
        kept = [stored for stored in facts if stored.casefold() != body["fact"].casefold()]
        if len(kept) == len(facts):
            return httpx.Response(404, json={"detail": "No such memory."})
        self.facts[body["topic"]] = kept
        return httpx.Response(204)


def _patched(route: FakeMemoryRoute) -> Any:
    client = functools.partial(httpx.AsyncClient, transport=httpx.MockTransport(route))
    return patch("payloads.agent.runtime.chat_server.Latin1HeaderClient", client)


async def _invoke(tool: FunctionTool, args_json: str) -> Any:
    return await tool.on_invoke_tool(MagicMock(), args_json)


def _call(route: FakeMemoryRoute, tool: str, **args: Any) -> str:
    tools = {t.name: t for t in memory_function_tools(user_route_client(MEMORY_ROUTE, USER))}
    with _patched(route):
        return str(asyncio.run(_invoke(tools[tool], json.dumps(args))))


class TestTopics:
    @pytest.mark.parametrize(
        ("topic", "slug"),
        [("Preferences", "preferences"), ("Café notes", "cafe-notes"), ("!!!", "general"), ("x" * 70, "x" * 64)],
    )
    def test_topic_slug(self, topic: str, slug: str) -> None:
        assert topic_slug(topic) == slug

    def test_forget_slug_takes_a_listed_path_and_rejects_others(self) -> None:
        assert forget_slug("/prefs.md") == "prefs"
        assert forget_slug("Prefs") == "prefs"
        assert forget_slug("/../x.md") is None
        assert forget_slug("/a/b.md") is None


class TestRemember:
    def test_saves_and_skips_duplicates(self) -> None:
        route = FakeMemoryRoute()
        assert _call(route, REMEMBER_TOOL_NAME, fact="Prefers tea", topic="Prefs") == "Remembered in /prefs.md."
        _call(route, REMEMBER_TOOL_NAME, fact="Likes  Python", topic="prefs")
        assert _call(route, REMEMBER_TOOL_NAME, fact="prefers TEA", topic="prefs").startswith("Already")
        assert route.facts == {"prefs": ["Prefers tea", "Likes Python"]}

    def test_calls_the_memory_route_as_the_user(self) -> None:
        route = FakeMemoryRoute()
        _call(route, REMEMBER_TOOL_NAME, fact="Prefers tea", topic="prefs")
        (request,) = route.requests
        assert (request.method, request.url.host, request.url.path) == ("POST", "localhost", f"{MEMORY_ROUTE}/remember")
        assert request.headers["x-forwarded-user"] == ALICE and "x-other" not in request.headers

    def test_refusals_show_the_routes_reason(self) -> None:
        route = FakeMemoryRoute(max_chars=10)
        assert "longer than 10" in _call(route, REMEMBER_TOOL_NAME, fact="x" * 11, topic="t")
        _call(route, REMEMBER_TOOL_NAME, fact="12345", topic="t")
        assert "is full" in _call(route, REMEMBER_TOOL_NAME, fact="67890", topic="t")

    @pytest.mark.parametrize("extra", [{"actor_id": "999@5678"}, {"scope": "chat"}])
    def test_the_model_cannot_choose_the_actor_or_a_scope(self, extra: dict[str, str]) -> None:
        route = FakeMemoryRoute()
        assert "invalid arguments" in _call(route, REMEMBER_TOOL_NAME, fact="x", topic="t", **extra)
        assert route.requests == []


class TestRecallAndList:
    def _route(self) -> FakeMemoryRoute:
        return FakeMemoryRoute(facts={"prefs": ["Prefers tea", "Likes Python"], "team": ["Tea club lead"]})

    def test_recall_groups_hits_by_topic(self) -> None:
        result = _call(self._route(), RECALL_TOOL_NAME, query="tea")
        assert result == "/prefs.md:\n  Prefers tea\n/team.md:\n  Tea club lead"

    def test_recall_without_hits(self) -> None:
        assert _call(self._route(), RECALL_TOOL_NAME, query="coffee") == "No relevant memories."

    def test_list_shows_topics_and_counts(self) -> None:
        assert _call(self._route(), LIST_MEMORIES_TOOL_NAME) == "/prefs.md: 2 facts\n/team.md: 1 fact"
        assert _call(FakeMemoryRoute(), LIST_MEMORIES_TOOL_NAME) == "No memories yet."


class TestForget:
    def test_removes_a_fact_by_path_or_topic(self) -> None:
        route = FakeMemoryRoute(facts={"prefs": ["Prefers tea", "Likes Python"]})
        assert _call(route, FORGET_TOOL_NAME, fact="prefers tea", topic="/prefs.md") == "Forgot it in /prefs.md."
        _call(route, FORGET_TOOL_NAME, fact="Likes Python", topic="prefs")
        assert route.facts == {"prefs": []}

    def test_unknown_fact_shows_what_is_stored(self) -> None:
        route = FakeMemoryRoute(facts={"prefs": ["Prefers tea"]})
        result = _call(route, FORGET_TOOL_NAME, fact="coffee", topic="prefs")
        assert result.startswith("Not found") and "Prefers tea" in result

    def test_unknown_topic(self) -> None:
        assert _call(FakeMemoryRoute(), FORGET_TOOL_NAME, fact="x", topic="prefs") == "No memories in /prefs.md."

    def test_rejects_a_path_outside_memory(self) -> None:
        route = FakeMemoryRoute()
        assert "invalid memory path" in _call(route, FORGET_TOOL_NAME, fact="x", topic="/../etc.md")
        assert route.requests == []


class TestFailures:
    @pytest.mark.parametrize("status", [401, 404, 500, 503])
    def test_route_errors_become_a_tool_message(self, status: int) -> None:
        assert "memory is unavailable" in _call(FakeMemoryRoute(status=status), RECALL_TOOL_NAME, query="tea")


class TestToolDefinitions:
    def test_definitions_do_not_depend_on_the_user(self) -> None:
        alice = memory_function_tools(user_route_client(MEMORY_ROUTE, USER))
        bob = memory_function_tools(user_route_client(MEMORY_ROUTE, {"x-forwarded-user": "999@5678"}))
        assert [(t.name, t.description, t.params_json_schema) for t in alice] == [
            (t.name, t.description, t.params_json_schema) for t in bob
        ]

    def test_no_tool_takes_a_scope(self) -> None:
        tools = memory_function_tools(user_route_client(MEMORY_ROUTE, USER))
        assert all("scope" not in t.params_json_schema["properties"] for t in tools)


class TestIndex:
    def test_empty_without_topics(self) -> None:
        assert render_memory_index([], 50) == ""

    def test_lists_topics_and_truncates(self) -> None:
        index = render_memory_index([MemoryTopic(topic=f"t{i}", facts=2) for i in range(3)], 2)
        assert "- /t0.md: 2 facts" in index and "/t2.md" not in index
        assert "...and 1 more" in index


class TestGuidance:
    def test_says_what_belongs_in_memory_and_rules_out_secrets(self) -> None:
        guidance = render_memory_guidance()
        assert "across the user's chats" in guidance and "not in memory" in guidance
        assert "Never save credentials" in guidance


class TestPrepareMemory:
    def _prepare(self, route: FakeMemoryRoute) -> Any:
        with _patched(route):
            return asyncio.run(prepare_memory(AgentMemoryRuntime(enabled=True), USER))

    def test_indexes_the_users_topics(self) -> None:
        memory = self._prepare(FakeMemoryRoute(facts={"prefs": ["x"]}))
        assert "/prefs.md: 1 fact" in memory.index
        assert {tool.name for tool in memory.tools} == {
            REMEMBER_TOOL_NAME,
            RECALL_TOOL_NAME,
            FORGET_TOOL_NAME,
            LIST_MEMORIES_TOOL_NAME,
        }

    def test_an_unreadable_memory_keeps_the_tools(self) -> None:
        memory = self._prepare(FakeMemoryRoute(status=503))
        assert memory.index == "" and memory.tools
