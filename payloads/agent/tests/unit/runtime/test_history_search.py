"""History tools (#685): when they are on, the calls to the chat UI server and what the model reads."""

from __future__ import annotations

import asyncio
import functools
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from payloads.agent.runtime.chat_server import ChatRouteClient
from payloads.agent.runtime.history_search import (
    HISTORY_READ_ROUTE,
    HISTORY_ROUTE,
    HISTORY_SEARCH_ROUTE,
    MAX_QUERY_CHARS,
    READ_HISTORY_TOOL_NAME,
    SEARCH_HISTORY_TOOL_NAME,
    ReadHistory,
    SearchHistory,
    prepare_history_search,
)

CHAT = "0b6c2d1e-7f3a-4c5b-9e8d-1a2b3c4d5e6f"
USER = {"x-forwarded-user": "alice-id", "x-forwarded-email": "alice@example.com"}
CLIENT = ChatRouteClient(f"http://localhost:3000{HISTORY_ROUTE.format(chat_id=CHAT)}", USER)
UI_SERVER = Path(__file__).parents[3] / "ui/overrides/server/src"
PAGE = {"turn": 2, "turns": 3, "offset": 0, "end": 8000, "chars": 9500, "text": "User: list tables"}


def _hits(request: httpx.Request) -> httpx.Response:
    assert request.url.path == HISTORY_SEARCH_ROUTE.format(chat_id=CHAT)
    assert json.loads(request.content) == {"query": "table_199"}
    assert request.headers["x-forwarded-user"] == "alice-id"
    return httpx.Response(200, json={"hits": [{"turn": 2, "at": 4100, "excerpt": "…table_199 has 42 rows…"}]})


def _no_hits(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"hits": []})


def _outline(request: httpx.Request) -> httpx.Response:
    assert request.url.path == HISTORY_READ_ROUTE.format(chat_id=CHAT)
    assert json.loads(request.content) == {}
    call = {"label": 'execute_sql {"query":"SHOW CATALOGS"}', "at": 812, "chars": 640}
    turn = {"turn": 1, "request": "User: overview", "toolCalls": [call], "moreToolCalls": 2, "chars": 12000}
    return httpx.Response(200, json={"outline": [turn]})


def _empty_outline(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"outline": []})


def _page(request: httpx.Request) -> httpx.Response:
    assert json.loads(request.content) == {"turn": 2, "offset": 0}
    return httpx.Response(200, json={"page": PAGE, "turns": 3})


def _last_page(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"page": {**PAGE, "offset": 8000, "end": 9500}, "turns": 3})


def _no_page(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"page": None, "turns": 3})


def _forbidden(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(403, json={"error": "forbidden"})


def _malformed(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"hits": [{"turn": "two"}], "outline": [{}]})


def _timeout(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectTimeout("timed out", request=request)


def _call(tool: type[SearchHistory | ReadHistory], handler: Any, **args: Any) -> str:
    client = functools.partial(httpx.AsyncClient, transport=httpx.MockTransport(handler))
    with patch("payloads.agent.runtime.chat_server.Latin1HeaderClient", client):
        return asyncio.run(tool(CLIENT)(MagicMock(), json.dumps(args)))


@pytest.fixture
def compaction_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLE_COMPACTION", "true")


class TestPrepare:
    def test_off_without_compaction(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ENABLE_COMPACTION", raising=False)
        assert prepare_history_search(CHAT, USER) is None

    @pytest.mark.parametrize("conversation_id", [None, "", "..", "a/b", "a.b", "x" * 129])
    @pytest.mark.usefixtures("compaction_on")
    def test_off_without_a_plain_conversation_id(self, conversation_id: str | None) -> None:
        assert prepare_history_search(conversation_id, USER) is None

    @pytest.mark.usefixtures("compaction_on")
    def test_both_tools_call_this_chat_as_the_user(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CHAT_APP_PORT", "4321")
        forwarded = {**USER, "x-forwarded-access-token": "secret", "cookie": "c"}
        prepared = prepare_history_search(CHAT, forwarded)
        assert prepared is not None
        assert [tool.name for tool in prepared.tools] == [SEARCH_HISTORY_TOOL_NAME, READ_HISTORY_TOOL_NAME]
        assert SEARCH_HISTORY_TOOL_NAME in prepared.guidance
        assert READ_HISTORY_TOOL_NAME in prepared.guidance
        expected = ChatRouteClient(f"http://localhost:4321/api/chat/{CHAT}/history", USER)
        assert all(tool.on_invoke_tool.client == expected for tool in prepared.tools)  # type: ignore[attr-defined]

    @pytest.mark.parametrize("route", [HISTORY_SEARCH_ROUTE, HISTORY_READ_ROUTE])
    def test_ui_server_serves_the_same_routes(self, route: str) -> None:
        mount, _, path = route.partition("/{chat_id}")
        assert f"app.use('{mount}', compactionRouter)" in (UI_SERVER / "index.ts").read_text(encoding="utf-8")
        assert f"'/:id{path}'" in (UI_SERVER / "routes/compaction.ts").read_text(encoding="utf-8")

    def test_headers_stay_out_of_the_client_repr(self) -> None:
        assert "alice" not in repr(CLIENT)


class TestSearchHistory:
    def test_returns_excerpts_with_their_turn_and_offset(self) -> None:
        assert _call(SearchHistory, _hits, query="table_199") == "Turn 2 at 4100: …table_199 has 42 rows…"

    def test_says_when_nothing_matches(self) -> None:
        assert _call(SearchHistory, _no_hits, query="x").startswith("No match")

    @pytest.mark.parametrize("handler", [_forbidden, _malformed, _timeout])
    def test_failures_read_as_unavailable(self, handler: Any) -> None:
        assert "unavailable" in _call(SearchHistory, handler, query="x")

    @pytest.mark.parametrize(
        "args", [{}, {"query": ""}, {"query": "x" * (MAX_QUERY_CHARS + 1)}, {"query": "x", "n": 1}]
    )
    def test_rejects_invalid_arguments(self, args: dict[str, Any]) -> None:
        assert _call(SearchHistory, _hits, **args).startswith("Error: give a query")


class TestReadHistory:
    def test_outlines_the_summarized_turns_and_their_tool_calls_without_a_turn(self) -> None:
        assert _call(ReadHistory, _outline).splitlines()[1:] == [
            "Turn 1: User: overview (12000 characters)",
            '  at 812: execute_sql {"query":"SHOW CATALOGS"} (640 characters)',
            "  … and 2 more tool calls",
        ]

    def test_says_when_nothing_is_summarized(self) -> None:
        assert _call(ReadHistory, _empty_outline) == "Nothing in this chat is summarized yet."

    def test_points_at_the_next_page(self) -> None:
        text = _call(ReadHistory, _page, turn=2)
        assert text.startswith("Turn 2 of 3, characters 0-8000 of 9500:\nUser: list tables")
        assert text.endswith(f"More: `{READ_HISTORY_TOOL_NAME}(turn=2, offset=8000)`.")

    def test_says_where_the_turn_ends(self) -> None:
        assert _call(ReadHistory, _last_page, turn=2, offset=8000).endswith("End of turn 2.")

    def test_names_the_summarized_turns_for_an_unknown_turn(self) -> None:
        assert _call(ReadHistory, _no_page, turn=9) == "No summarized turn 9; the summary replaced turns 1 to 3."

    @pytest.mark.parametrize("handler", [_forbidden, _malformed, _timeout])
    def test_failures_read_as_unavailable(self, handler: Any) -> None:
        assert "unavailable" in _call(ReadHistory, handler)

    @pytest.mark.parametrize("args", [{"turn": 0}, {"turn": 1, "offset": -1}, {"turn": "one"}, {"page": 1}])
    def test_rejects_invalid_arguments(self, args: dict[str, Any]) -> None:
        assert _call(ReadHistory, _page, **args).startswith("Error: turn must be")
