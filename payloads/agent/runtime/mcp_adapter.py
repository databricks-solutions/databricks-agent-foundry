"""
MCP Adapter
============
Exposes the agent as a single MCP tool (``ask_agent``) using the
official ``mcp`` SDK (``mcp.server.fastmcp.FastMCP``).

Routes are registered directly on the FastAPI app (via ``Mount``,
not ``app.mount``) for compatibility with the Databricks Apps proxy.

Transport notes
---------------
- ``stateless_http=True``: each request creates a fresh transport
  (no session tracking) — appropriate because ``run_agent()`` is a
  single request/response, not a multi-turn conversation.

- ``json_response=True``: forces the MCP transport to return plain
  ``application/json`` instead of SSE (``text/event-stream``).
  The Databricks Apps proxy re-encodes responses on ``/api/*`` routes
  (OAuth enforcement, zstd compression) which corrupts SSE streams,
  causing ``'ascii' codec can't encode characters`` 502 errors.
  JSON responses survive the proxy without issues.

  This has **no functional impact** today: ``run_agent()`` calls
  ``Runner.run()`` (not ``run_streamed()``), so the full response is
  returned as a single string regardless of transport.  When streaming
  support is added to ``agent_runner``, revisit this setting — either
  switch back to SSE or move the MCP endpoint outside ``/api/*``.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.routing import Mount
from starlette.types import Receive, Scope, Send

from payloads.agent.runtime.agent_runner import run_agent, set_request_headers

logger = logging.getLogger(__name__)

mcp = FastMCP("AgentMCP", stateless_http=True, json_response=True)


@mcp.tool()
async def ask_agent(question: str) -> str:
    """Ask the agent a question and get a response."""
    return await run_agent(question, source="mcp")


class _McpAsgiHandler:
    """ASGI wrapper that forwards OBO headers to the agent runner context."""

    def __init__(self, session_manager: StreamableHTTPSessionManager) -> None:
        self._sm = session_manager

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = {k.decode(): v.decode() for k, v in scope.get("headers", [])}
            set_request_headers(headers)
        await self._sm.handle_request(scope, receive, send)


def register_mcp_routes(app: FastAPI) -> None:
    """Register MCP Streamable-HTTP route directly on the FastAPI app."""
    session_manager = StreamableHTTPSessionManager(
        app=mcp._mcp_server,
        event_store=mcp._event_store,
        json_response=True,
        stateless=True,
    )
    mcp._session_manager = session_manager

    app.routes.insert(0, Mount("/api/mcp", app=_McpAsgiHandler(session_manager)))

    original_lifespan = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def _combined_lifespan(
        app_instance: FastAPI,
    ) -> AsyncIterator[dict[str, Any]]:
        async with session_manager.run():
            if original_lifespan is not None:
                async with original_lifespan(app_instance) as state:
                    yield dict(state) if state else {}
            else:
                yield {}

    app.router.lifespan_context = _combined_lifespan

    logger.info("[mcp] route registered at /api/mcp")
