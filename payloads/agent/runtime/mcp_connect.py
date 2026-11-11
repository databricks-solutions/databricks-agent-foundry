"""Build and connect the request's MCP servers, recording the unavailable ones.

Shared by the ``@invoke``/``@stream`` handlers in
:mod:`payloads.agent.runtime.agent` and the MCP/A2A runner in
:mod:`payloads.agent.runtime.agent_runner`.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import mlflow
from agents.mcp import MCPServerManager

from payloads.agent.runtime.brick_mcp_servers import BRICK_SERVER_NAME_PREFIX, build_mcp_servers_with_status
from payloads.agent.runtime.gateway_mcp import GatewayMcpServer, apply_gateway_tool_rules, unavailable_service
from payloads.agent.runtime.mcp_availability import TRACE_TAG, UnavailableServer, trace_label
from payloads.agent.runtime.mcp_deferred_connect import can_defer_connect, deferred_connect
from payloads.agent.runtime.mcp_tools_cache import prewarm_tool_lists

if TYPE_CHECKING:
    from databricks.sdk import WorkspaceClient

    from payloads.agent.runtime.gateway_selection import Selections
    from payloads.agent.runtime.mcp_consumption import CachedManual

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ConnectedMcp:
    """The request's MCP servers, for the lifetime of :func:`managed_mcp_servers`.

    * *servers* -- connected MCP server instances (may be empty when no
      MCP-served tool types are configured or all connections fail).
    * *unavailable* -- configured servers this request could not mount.
    * *request_cache* -- empty mutable ``dict`` used as the
      ``per_request`` cache by
      :func:`payloads.agent.runtime.mcp_consumption.collect_prompt_manuals`.
      It lives exactly as long as the async-context, so cache entries are
      dropped automatically on teardown.
    """

    servers: list[Any]
    unavailable: tuple[UnavailableServer, ...]
    request_cache: dict[tuple[str, str], CachedManual]


@asynccontextmanager
async def _measure_mcp_connect(manager: MCPServerManager, *, requested: int) -> AsyncGenerator[Any, None]:
    """Wrap ``MCPServerManager`` in an ``agent.mcp_connect`` MLflow span.

    The span surrounds ``__aenter__`` only -- once the manager is
    connected we end the span immediately, then re-enter the manager
    via the surrounding ``async with`` body.  ``__aexit__`` runs after
    the body without a wrapping span (cleanup time is not interesting
    for cold-start analysis).

    Splitting the span this way is necessary because ``__aenter__`` is
    where the actual connect work (TLS + OAuth + ``initialize``)
    happens; spanning the full lifetime would also include every
    ``call_tool`` made during the agent's run, which would defeat the
    purpose of bisecting cold-start latency.

    Falls back to a simple pass-through (no span) if the SDK ever
    swaps ``MCPServerManager`` for an object whose ``__aenter__``
    raises -- :func:`mlflow.start_span` would still close cleanly via
    its ``with`` exit hook, and the exception propagates so the
    caller sees the same error it would have without the wrapper.
    """
    with mlflow.start_span(name="agent.mcp_connect", span_type="TASK") as span:
        span.set_attribute("requested_servers", requested)
        connected = await manager.__aenter__()
        span.set_attribute("active_servers", len(connected.active_servers))
    try:
        yield connected
    finally:
        await manager.__aexit__(None, None, None)


def _connect_failure(server: Any) -> UnavailableServer | None:
    if isinstance(server, GatewayMcpServer):
        return unavailable_service(server.candidate, "connect_failed")
    return UnavailableServer(server.name.removeprefix(BRICK_SERVER_NAME_PREFIX), "connect_failed")


def _connect_failures(failed: Sequence[Any]) -> tuple[UnavailableServer, ...]:
    """*failed* servers as unavailable entries; discovered gateway services are left out."""
    entries = (_connect_failure(server) for server in failed)
    return tuple(entry for entry in entries if entry is not None)


@asynccontextmanager
async def managed_mcp_servers(
    config: dict[str, Any],
    sp_ws: WorkspaceClient,
    selections: Selections | None = None,
) -> AsyncGenerator[ConnectedMcp, None]:
    """Connect Databricks Managed MCP servers for the request lifetime.

    *selections* are the request's sidebar picks; only gateway mode reads
    them.  Unavailable servers are tagged on the current trace.
    """
    async with _connected_mcp_servers(config, sp_ws, selections) as mcp:
        if mcp.unavailable:
            mlflow.update_current_trace(tags={TRACE_TAG: trace_label(mcp.unavailable)})
        yield mcp


@asynccontextmanager
async def _connected_mcp_servers(
    config: dict[str, Any],
    sp_ws: WorkspaceClient,
    selections: Selections | None,
) -> AsyncGenerator[ConnectedMcp, None]:
    with mlflow.start_span(
        name="agent.build_mcp_servers",
        span_type="TASK",
    ) as span:
        build = build_mcp_servers_with_status(config, sp_ws, selections=selections)
        span.set_attribute("server_count", len(build.servers))
    request_cache: dict[tuple[str, str], CachedManual] = {}
    if not build.servers:
        yield ConnectedMcp([], build.unavailable, request_cache)
        return
    if can_defer_connect(build.servers):
        async with deferred_connect(build.servers) as deferred:
            ruled = await apply_gateway_tool_rules(config, deferred)
            yield ConnectedMcp(ruled.servers, (*build.unavailable, *ruled.unavailable), request_cache)
        return

    requested = len(build.servers)
    # ``connect_in_parallel=True`` is critical for cold-start latency:
    # the SDK default runs ``server.connect()`` (TLS + OAuth +
    # MCP ``initialize``) sequentially, which adds N x ~hundreds-of-ms
    # to every request that hits a fresh process.  The SDK's parallel
    # path (see :class:`agents.mcp.MCPServerManager._connect_all_parallel`)
    # uses ``asyncio.gather`` with per-server worker tasks that preserve
    # the connect/cleanup task affinity required by MCP's ``anyio``
    # cancel scopes.  ``drop_failed_servers=True`` keeps failure
    # semantics identical -- a partial failure still surfaces only
    # connected servers to the agent.
    async with _measure_mcp_connect(
        MCPServerManager(
            build.servers,
            drop_failed_servers=True,
            connect_in_parallel=True,
        ),
        requested=requested,
    ) as manager:
        active = len(manager.active_servers)
        if active < requested:
            logger.warning(
                "[mcp] %d of %d MCP server(s) failed to connect",
                requested - active,
                requested,
            )
        if active:
            logger.info("[mcp] %d MCP server(s) connected", active)
        # Listed concurrently first, so the rules read every tool list from cache.
        await prewarm_tool_lists(manager.active_servers)
        ruled = await apply_gateway_tool_rules(config, manager.active_servers)
        unavailable = (*build.unavailable, *_connect_failures(manager.failed_servers), *ruled.unavailable)
        yield ConnectedMcp(ruled.servers, unavailable, request_cache)
