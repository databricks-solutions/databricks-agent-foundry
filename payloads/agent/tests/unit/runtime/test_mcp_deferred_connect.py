"""Tests for :mod:`payloads.agent.runtime.mcp_deferred_connect`."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp.types import CallToolResult, Tool

from agent_foundry.contracts.mcp import PromptsConfig, ResourcesConfig
from payloads.agent.runtime.mcp_deferred_connect import can_defer_connect, deferred_connect
from payloads.agent.runtime.mcp_tools_cache import CachedTools, lookup_tools, store_tools
from payloads.agent.runtime.mcp_wrapper import FilteredMCPServerStreamableHttp

_URL = "https://example.invalid/mcp"


def _server(
    *,
    cached: bool = True,
    prompts: PromptsConfig | None = None,
    resources: ResourcesConfig | None = None,
) -> FilteredMCPServerStreamableHttp:
    if cached:
        store_tools((_URL, "sp"), [Tool(name="a", inputSchema={"type": "object"})])
    return FilteredMCPServerStreamableHttp(
        params={"url": _URL},
        server_url=_URL,
        cache_scope="sp",
        prompts=prompts,
        resources=resources,
        cache_tools_list=True,
        name="test-server",
    )


def _stub_lifecycle(server: FilteredMCPServerStreamableHttp, connect: AsyncMock) -> AsyncMock:
    cleanup = AsyncMock()
    server.connect = connect  # type: ignore[method-assign]
    server.cleanup = cleanup  # type: ignore[method-assign]
    return cleanup


class TestCanDeferConnect:
    def test_all_cached_servers_defer(self) -> None:
        assert can_defer_connect([_server()])

    def test_empty_list_does_not_defer(self) -> None:
        assert not can_defer_connect([])

    def test_cache_miss_does_not_defer(self) -> None:
        assert not can_defer_connect([_server(cached=False)])

    def test_prompts_enabled_does_not_defer(self) -> None:
        assert not can_defer_connect([_server(prompts=PromptsConfig(enabled=True))])

    def test_resources_enabled_does_not_defer(self) -> None:
        assert not can_defer_connect([_server(resources=ResourcesConfig(enabled=True))])


@pytest.mark.asyncio
class TestDeferredConnect:
    async def test_yields_before_connect_finishes(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def blocking_connect() -> None:
            started.set()
            await release.wait()

        server = _server()
        cleanup = _stub_lifecycle(server, AsyncMock(side_effect=blocking_connect))

        async with deferred_connect([server]) as servers:
            assert servers == [server]
            await asyncio.wait_for(started.wait(), timeout=1)
            release.set()

        cleanup.assert_awaited_once()

    async def test_tool_call_waits_for_connect(self) -> None:
        release = asyncio.Event()
        server = _server()
        _stub_lifecycle(server, AsyncMock(side_effect=release.wait))

        result = MagicMock(spec=CallToolResult)
        with patch.object(server, "_call_tool_impl", AsyncMock(return_value=result)) as impl:
            async with deferred_connect([server]):
                call = asyncio.create_task(server.call_tool("a", {}))
                await asyncio.sleep(0.01)
                impl.assert_not_awaited()
                release.set()
                assert await call is result

    async def test_exit_waits_for_in_flight_connect_before_cleanup(self) -> None:
        order: list[str] = []

        async def slow_connect() -> None:
            await asyncio.sleep(0.01)
            order.append("connected")

        server = _server()
        cleanup = _stub_lifecycle(server, AsyncMock(side_effect=slow_connect))
        cleanup.side_effect = lambda: order.append("cleaned")

        async with deferred_connect([server]):
            pass

        assert order == ["connected", "cleaned"]

    async def test_connect_failure_does_not_break_the_request(self) -> None:
        server = _server()
        cleanup = _stub_lifecycle(server, AsyncMock(side_effect=RuntimeError("down")))

        async with deferred_connect([server]):
            await asyncio.sleep(0.01)

        cleanup.assert_awaited_once()

    async def test_connect_failure_forgets_cached_tools(self) -> None:
        """The next request then connects before the run and reports the server."""
        healthy = _server()
        _stub_lifecycle(healthy, AsyncMock())
        async with deferred_connect([healthy]):
            pass
        assert healthy.has_cached_tools()

        failing = _server()
        _stub_lifecycle(failing, AsyncMock(side_effect=RuntimeError("down")))
        async with deferred_connect([failing]):
            pass
        assert not can_defer_connect([failing])

    async def test_refreshes_stale_tool_lists_after_connecting(self, monkeypatch: pytest.MonkeyPatch) -> None:
        now = [1000.0]
        monkeypatch.setattr(time, "monotonic", lambda: now[0])
        server = _server()
        now[0] += 301
        fresh = [Tool(name="b", inputSchema={"type": "object"})]
        server.session = MagicMock(list_tools=AsyncMock(return_value=SimpleNamespace(tools=fresh)))
        _stub_lifecycle(server, AsyncMock())

        async with deferred_connect([server]):
            served = await server.list_tools()

        assert [t.name for t in served] == ["a"]
        server.session.list_tools.assert_awaited_once()
        assert lookup_tools((_URL, "sp")) == CachedTools(tools=fresh, stale=False)
