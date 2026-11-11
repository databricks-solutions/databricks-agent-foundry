"""Connect MCP servers in the background when their tool lists are cached.

On a follow-up turn every brick server's ``tools/list`` is usually in the
process cache (:mod:`payloads.agent.runtime.mcp_tools_cache`), so the model
can start before any MCP session exists.  :func:`deferred_connect` starts
the connect as a background task instead of blocking on it: it overlaps
the first model call, and tool calls await it
(:meth:`FilteredMCPServerStreamableHttp.call_tool`).

``MCPServerManager(connect_in_parallel=True)`` runs each server's connect
and cleanup in a dedicated worker task, so starting ``connect_all`` in one
task and calling ``cleanup_all`` from the request task keeps the
connect/cleanup task affinity MCP's ``anyio`` cancel scopes require.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager

import mlflow
from agents.mcp import MCPServer, MCPServerManager

from payloads.agent.runtime.mcp_wrapper import FilteredMCPServerStreamableHttp

logger = logging.getLogger(__name__)


def can_defer_connect(servers: Sequence[FilteredMCPServerStreamableHttp]) -> bool:
    """Whether the run can start before *servers* are connected.

    Requires a cached tool list for every server, and no prompts or
    resources: the consumption layer reads those over the session before
    the agent is built.
    """
    return bool(servers) and all(
        server.has_cached_tools() and not server.prompts.enabled and not server.resources.enabled for server in servers
    )


@asynccontextmanager
async def deferred_connect(
    servers: list[FilteredMCPServerStreamableHttp],
) -> AsyncGenerator[list[FilteredMCPServerStreamableHttp], None]:
    """Yield *servers* immediately while they connect in the background.

    A server that fails to connect stays in the list (the model already saw
    its cached tools); its tool calls then fail with the SDK's
    "server not initialized" error instead of the server being dropped.
    Its cached tool list is dropped, so the next request connects before
    the run and reports the server as unavailable.
    """
    manager = MCPServerManager(servers, drop_failed_servers=True, connect_in_parallel=True)
    pending = asyncio.create_task(_connect_traced(manager, len(servers)))
    for server in servers:
        server.defer_connect(pending)
    try:
        yield servers
    finally:
        await _settle(pending)
        await manager.cleanup_all()


async def _connect_traced(manager: MCPServerManager, requested: int) -> None:
    with mlflow.start_span(name="agent.mcp_connect", span_type="TASK") as span:
        span.set_attribute("requested_servers", requested)
        span.set_attribute("deferred", True)
        await manager.connect_all()
        span.set_attribute("active_servers", len(manager.active_servers))
    failed = requested - len(manager.active_servers)
    if failed:
        logger.warning("[mcp] %d of %d deferred MCP server(s) failed to connect", failed, requested)
    for server in manager.failed_servers:
        if isinstance(server, FilteredMCPServerStreamableHttp):
            server.forget_cached_tools()
    await _refresh_stale_tools(manager.active_servers)


async def _refresh_stale_tools(servers: Sequence[MCPServer]) -> None:
    """Refetch the stale tool lists the run started on, for the next request."""
    wrapped = [server for server in servers if isinstance(server, FilteredMCPServerStreamableHttp)]
    results = await asyncio.gather(*(server.refresh_stale_tools() for server in wrapped), return_exceptions=True)
    for server, result in zip(wrapped, results, strict=True):
        if isinstance(result, Exception):
            logger.warning("[mcp] tools refresh failed for %s: %s", server.name, result)


async def _settle(pending: asyncio.Task[None]) -> None:
    """Wait for the connect to finish before cleanup, and reap its error.

    Not cancelled: a cancelled ``connect_all`` cleans up on its own and
    ``cleanup_all`` would then run a second cleanup outside the worker
    task.  The wait is bounded by the manager's connect timeout and in
    practice over long before the run ends.  ``asyncio.wait`` never raises
    the task's own error but still propagates cancellation of the caller.
    """
    await asyncio.wait({pending})
    if not pending.cancelled() and pending.exception() is not None:
        logger.warning("[mcp] deferred connect failed: %s", pending.exception())
