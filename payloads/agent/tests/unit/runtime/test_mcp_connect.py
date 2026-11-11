"""Unavailable MCP servers collected by :func:`managed_mcp_servers`."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import ExitStack, asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from payloads.agent.runtime.gateway_mcp import GatewayMcpServer
from payloads.agent.runtime.gateway_resolve import Candidate, Origin
from payloads.agent.runtime.mcp_availability import McpBuild, UnavailableServer
from payloads.agent.runtime.mcp_connect import managed_mcp_servers

MODULE = "payloads.agent.runtime.mcp_connect"
BUILD_SKIP = UnavailableServer("broken", "invalid_state")
RULE_SKIP = UnavailableServer("main.tools.a", "tool_name_collision")


class _FakeManager:
    def __init__(self, active: list[Any], failed: list[Any]) -> None:
        self.active_servers = active
        self.failed_servers = failed

    async def __aenter__(self) -> _FakeManager:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


def _brick(sub_name: str) -> MagicMock:
    server = MagicMock()
    server.name = f"mcp-brick-{sub_name}"
    return server


def _gateway(fqn: str, origin: Origin) -> GatewayMcpServer:
    candidate = Candidate(fqn, "sp", origin)
    return GatewayMcpServer(params={"url": f"https://h/{fqn}"}, candidate=candidate, name=f"gateway-{fqn}")


async def _connect(build: McpBuild, manager: _FakeManager) -> tuple[list[Any], tuple[UnavailableServer, ...]]:
    with ExitStack() as stack:
        stack.enter_context(patch(f"{MODULE}.build_mcp_servers_with_status", return_value=build))
        stack.enter_context(patch(f"{MODULE}.can_defer_connect", return_value=False))
        stack.enter_context(patch(f"{MODULE}.MCPServerManager", return_value=manager))
        stack.enter_context(patch(f"{MODULE}.prewarm_tool_lists", new=AsyncMock()))
        async with managed_mcp_servers({}, MagicMock()) as mcp:
            return mcp.servers, mcp.unavailable


@pytest.mark.asyncio
async def test_connect_failures_follow_build_skips() -> None:
    ok, down = _brick("genie"), _brick("jira")
    selected, discovered = _gateway("main.tools.a", "selected"), _gateway("main.tools.b", "discovered")
    build = McpBuild([ok, down, selected, discovered], (BUILD_SKIP,))

    servers, unavailable = await _connect(build, _FakeManager([ok], [down, selected, discovered]))

    assert servers == [ok]
    assert unavailable == (
        BUILD_SKIP,
        UnavailableServer("jira", "connect_failed"),
        UnavailableServer("main.tools.a", "connect_failed"),
    )


@pytest.mark.asyncio
async def test_no_servers_still_reports_build_skips() -> None:
    servers, unavailable = await _connect(McpBuild([], (BUILD_SKIP,)), _FakeManager([], []))

    assert servers == []
    assert unavailable == (BUILD_SKIP,)


@pytest.mark.asyncio
async def test_unavailable_tagged_on_trace() -> None:
    with patch(f"{MODULE}.mlflow.update_current_trace") as update_trace:
        await _connect(McpBuild([], (BUILD_SKIP,)), _FakeManager([], []))

    update_trace.assert_called_once_with(tags={"mcp.unavailable": "broken|invalid_state"})


@pytest.mark.asyncio
async def test_all_available_leaves_trace_untagged() -> None:
    with patch(f"{MODULE}.mlflow.update_current_trace") as update_trace:
        await _connect(McpBuild([]), _FakeManager([], []))

    update_trace.assert_not_called()


@asynccontextmanager
async def _connected_now(servers: list[Any]) -> AsyncIterator[list[Any]]:
    yield servers


@pytest.mark.asyncio
async def test_deferred_connect_still_applies_tool_rules() -> None:
    kept, ruled_out = _gateway("main.tools.b", "pinned"), _gateway("main.tools.a", "pinned")
    rules = AsyncMock(return_value=McpBuild([kept], (RULE_SKIP,)))
    with ExitStack() as stack:
        stack.enter_context(
            patch(f"{MODULE}.build_mcp_servers_with_status", return_value=McpBuild([kept, ruled_out], (BUILD_SKIP,)))
        )
        stack.enter_context(patch(f"{MODULE}.can_defer_connect", return_value=True))
        stack.enter_context(patch(f"{MODULE}.deferred_connect", new=_connected_now))
        stack.enter_context(patch(f"{MODULE}.apply_gateway_tool_rules", new=rules))
        async with managed_mcp_servers({}, MagicMock()) as mcp:
            servers, unavailable = mcp.servers, mcp.unavailable

    rules.assert_awaited_once_with({}, [kept, ruled_out])
    assert servers == [kept]
    assert unavailable == (BUILD_SKIP, RULE_SKIP)


@pytest.mark.asyncio
async def test_tool_lists_prewarmed_before_tool_rules() -> None:
    ok = _brick("genie")
    calls = MagicMock()
    calls.prewarm = AsyncMock()
    calls.rules = AsyncMock(return_value=McpBuild([ok]))
    with ExitStack() as stack:
        stack.enter_context(patch(f"{MODULE}.build_mcp_servers_with_status", return_value=McpBuild([ok])))
        stack.enter_context(patch(f"{MODULE}.can_defer_connect", return_value=False))
        stack.enter_context(patch(f"{MODULE}.MCPServerManager", return_value=_FakeManager([ok], [])))
        stack.enter_context(patch(f"{MODULE}.prewarm_tool_lists", new=calls.prewarm))
        stack.enter_context(patch(f"{MODULE}.apply_gateway_tool_rules", new=calls.rules))
        async with managed_mcp_servers({}, MagicMock()):
            pass

    assert [name for name, *_ in calls.mock_calls] == ["prewarm", "rules"]
    calls.prewarm.assert_awaited_once_with([ok])
