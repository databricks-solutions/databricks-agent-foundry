"""Tests for :mod:`payloads.agent.runtime.mcp_tools_cache`.

The wrapper tests drive the *real* SDK ``list_tools`` against a fake
MCP session: the cache seeds the SDK's private per-instance cache, and
these tests are what fails loudly if an openai-agents upgrade changes it.
"""

from __future__ import annotations

import logging
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp.types import Tool

from payloads.agent.runtime import mcp_tools_cache
from payloads.agent.runtime.mcp_tools_cache import (
    SERVICE_PRINCIPAL_SCOPE,
    CachedTools,
    identity_scope,
    lookup_tools,
    prewarm_tool_lists,
    store_tools,
    ttl_seconds,
)
from payloads.agent.runtime.mcp_wrapper import FilteredMCPServerStreamableHttp

_URL = "https://example.invalid/mcp"


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(time, "monotonic", fake)
    return fake


def _tool(name: str) -> Tool:
    return Tool(name=name, inputSchema={"type": "object", "properties": {}, "required": ["q"]})


def _connected_server(tools: list[Tool], *, cache_scope: str, url: str = _URL) -> FilteredMCPServerStreamableHttp:
    """Wrapper whose SDK base talks to a fake session returning *tools*."""
    server = FilteredMCPServerStreamableHttp(
        params={"url": url},
        server_url=url,
        cache_scope=cache_scope,
        cache_tools_list=True,
        name="test-server",
    )
    server.session = MagicMock(list_tools=AsyncMock(return_value=SimpleNamespace(tools=tools)))
    return server


class TestIdentityScope:
    def test_no_token_is_service_principal(self) -> None:
        assert identity_scope(None) == SERVICE_PRINCIPAL_SCOPE
        assert identity_scope("") == SERVICE_PRINCIPAL_SCOPE

    def test_token_is_digested_not_stored(self) -> None:
        scope = identity_scope("secret-token")
        assert "secret-token" not in scope
        assert len(scope) == 64

    def test_distinct_tokens_get_distinct_scopes(self) -> None:
        assert identity_scope("token-a") != identity_scope("token-b")


class TestTtlSeconds:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [(None, 300.0), ("60", 60.0), ("0", 0.0), ("abc", 300.0), ("-5", 300.0)],
    )
    def test_resolution(self, monkeypatch: pytest.MonkeyPatch, raw: str | None, expected: float) -> None:
        if raw is None:
            monkeypatch.delenv("MCP_TOOLS_CACHE_TTL_SECONDS", raising=False)
        else:
            monkeypatch.setenv("MCP_TOOLS_CACHE_TTL_SECONDS", raw)
        assert ttl_seconds() == expected


class TestStoreAndGet:
    def test_round_trip(self) -> None:
        tools: list[Any] = [_tool("a")]
        store_tools((_URL, "sp"), tools)
        assert lookup_tools((_URL, "sp")) == CachedTools(tools=tools, stale=False)

    def test_miss_for_other_scope(self) -> None:
        store_tools((_URL, "sp"), [_tool("a")])
        assert lookup_tools((_URL, "user")) is None

    def test_entry_past_the_ttl_is_stale(self, clock: _Clock) -> None:
        store_tools((_URL, "sp"), [_tool("a")])
        clock.now += 301
        cached = lookup_tools((_URL, "sp"))
        assert cached is not None and cached.stale

    def test_entry_past_the_max_age_is_dropped(self, clock: _Clock) -> None:
        store_tools((_URL, "sp"), [_tool("a")])
        clock.now += 3601
        assert lookup_tools((_URL, "sp")) is None

    def test_ttl_beyond_the_max_age_stays_fresh(self, clock: _Clock, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MCP_TOOLS_CACHE_TTL_SECONDS", "7200")
        store_tools((_URL, "sp"), [_tool("a")])
        clock.now += 7000
        assert lookup_tools((_URL, "sp")) == CachedTools(tools=[_tool("a")], stale=False)

    def test_zero_ttl_disables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MCP_TOOLS_CACHE_TTL_SECONDS", "0")
        store_tools((_URL, "sp"), [_tool("a")])
        assert lookup_tools((_URL, "sp")) is None

    def test_oldest_entry_evicted_when_full(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(mcp_tools_cache, "_MAX_ENTRIES", 2)
        for scope in ("one", "two", "three"):
            store_tools((_URL, scope), [_tool("a")])
        assert lookup_tools((_URL, "one")) is None
        assert lookup_tools((_URL, "three")) is not None


@pytest.mark.asyncio
class TestWrapperIntegration:
    async def test_second_request_skips_upstream_fetch(self) -> None:
        first = _connected_server([_tool("a")], cache_scope="sp")
        await first.list_tools()
        second = _connected_server([_tool("a")], cache_scope="sp")

        tools = await second.list_tools()

        assert [t.name for t in tools] == ["a"]
        second.session.list_tools.assert_not_awaited()  # type: ignore[union-attr]

    async def test_cache_hit_seeds_sdk_required_param_validation(self) -> None:
        await _connected_server([_tool("a")], cache_scope="sp").list_tools()
        second = _connected_server([_tool("a")], cache_scope="sp")

        await second.list_tools()

        assert second._tools_list is not None
        assert [t.name for t in second._tools_list] == ["a"]
        assert second._cache_dirty is False

    async def test_other_identity_fetches_its_own_list(self) -> None:
        await _connected_server([_tool("a")], cache_scope="user-a").list_tools()
        other = _connected_server([_tool("b")], cache_scope="user-b")

        tools = await other.list_tools()

        assert [t.name for t in tools] == ["b"]
        other.session.list_tools.assert_awaited_once()  # type: ignore[union-attr]

    async def test_cache_hit_needs_no_session(self) -> None:
        await _connected_server([_tool("a")], cache_scope="sp").list_tools()
        unconnected = _connected_server([_tool("a")], cache_scope="sp")
        unconnected.session = None

        assert unconnected.has_cached_tools()
        assert [t.name for t in await unconnected.list_tools()] == ["a"]

    async def test_empty_scope_never_caches(self) -> None:
        await _connected_server([_tool("a")], cache_scope="").list_tools()
        assert lookup_tools((_URL, "")) is None

    async def test_stale_list_served_while_unconnected(self, clock: _Clock) -> None:
        await _connected_server([_tool("a")], cache_scope="sp").list_tools()
        clock.now += 301
        unconnected = _connected_server([_tool("b")], cache_scope="sp")
        unconnected.session = None

        assert unconnected.has_cached_tools()
        assert [t.name for t in await unconnected.list_tools()] == ["a"]

    async def test_stale_list_refetched_once_connected(self, clock: _Clock) -> None:
        await _connected_server([_tool("a")], cache_scope="sp").list_tools()
        clock.now += 301
        connected = _connected_server([_tool("b")], cache_scope="sp")

        tools = await connected.list_tools()

        assert [t.name for t in tools] == ["b"]
        assert lookup_tools((_URL, "sp")) == CachedTools(tools=[_tool("b")], stale=False)


@pytest.mark.asyncio
class TestRefreshStaleTools:
    async def test_refetches_a_stale_list_seeded_into_the_sdk(self, clock: _Clock) -> None:
        await _connected_server([_tool("a")], cache_scope="sp").list_tools()
        clock.now += 301
        server = _connected_server([_tool("b")], cache_scope="sp")
        server.session, session = None, server.session
        await server.list_tools()
        server.session = session

        await server.refresh_stale_tools()

        assert lookup_tools((_URL, "sp")) == CachedTools(tools=[_tool("b")], stale=False)

    async def test_leaves_a_fresh_list_alone(self) -> None:
        await _connected_server([_tool("a")], cache_scope="sp").list_tools()
        server = _connected_server([_tool("b")], cache_scope="sp")

        await server.refresh_stale_tools()

        server.session.list_tools.assert_not_awaited()  # type: ignore[union-attr]


@pytest.mark.asyncio
class TestPrewarm:
    async def test_lists_every_server(self) -> None:
        servers = [MagicMock(list_tools=AsyncMock(return_value=[])) for _ in range(3)]

        await prewarm_tool_lists(servers)

        for server in servers:
            server.list_tools.assert_awaited_once()

    async def test_failure_is_logged_not_raised(self, caplog: pytest.LogCaptureFixture) -> None:
        failing = MagicMock(list_tools=AsyncMock(side_effect=RuntimeError("boom")))
        failing.name = "broken"
        healthy = MagicMock(list_tools=AsyncMock(return_value=[]))

        with caplog.at_level(logging.WARNING):
            await prewarm_tool_lists([failing, healthy])

        assert "broken" in caplog.text
        healthy.list_tools.assert_awaited_once()
