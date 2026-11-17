"""MCP calls against a workspace UC URL (not a Databricks App).

App URLs (``*.databricksapps.com``) reject a PAT, so tests go through the
UC gateway (``/api/2.0/mcp/external/<connection>``). The gateway accepts
the workspace OAuth/PAT Bearer from ``WorkspaceClient.config.authenticate()``;
an authenticated caller without ``USE CONNECTION`` gets 403, and failures on
the gateway -> App hop come back with a JSON ``error_code`` / ``message``.

``DatabricksMCPClient`` / ``DatabricksOAuthClientProvider`` are not used:
their helper is built with ``server_url=""``, so any 401 triggers metadata
discovery against an empty URL and httpx raises ``UnsupportedProtocol``,
hiding the gateway's error body.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from functools import partial
from typing import Any, AsyncIterator, Callable, Coroutine, Final, TypeVar

_T = TypeVar("_T")

_ACCEPT: Final = "application/json, text/event-stream"
_ERROR_BODY_LIMIT: Final = 500
_INITIALIZE_PROBE: Final[dict[str, Any]] = {
    "jsonrpc": "2.0",
    "id": 0,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "agent-foundry-acceptance", "version": "0"},
    },
}


class McpGatewayError(RuntimeError):
    """The MCP endpoint rejected the request; the message carries its response body."""


def _auth_headers(ws: Any) -> dict[str, str]:
    return dict(ws.config.authenticate())


def require_gateway_ok(server_url: str, headers: dict[str, str], *, timeout_seconds: float = 60) -> None:
    """POST one MCP ``initialize`` and raise with the response body on HTTP errors."""
    import httpx

    response = httpx.post(
        server_url,
        headers={**headers, "Accept": _ACCEPT},
        json=_INITIALIZE_PROBE,
        timeout=timeout_seconds,
    )
    if response.status_code >= 400:
        raise McpGatewayError(f"{response.status_code} from {server_url}: {response.text[:_ERROR_BODY_LIMIT]}")


def _is_mcp_v2() -> bool:
    import mcp

    return hasattr(mcp, "Client")


@asynccontextmanager
async def _v1_session(server_url: str, headers: dict[str, str]) -> AsyncIterator[Any]:
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(server_url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


@asynccontextmanager
async def _v2_session(server_url: str, headers: dict[str, str]) -> AsyncIterator[Any]:
    # mcp 2.x moved auth onto an httpx2 client and replaced ClientSession with Client.
    from mcp import Client  # type: ignore[attr-defined]
    from mcp.client.streamable_http import (  # type: ignore[attr-defined]
        create_mcp_http_client,
        streamable_http_client,
    )

    async with create_mcp_http_client(headers=headers) as http_client:
        async with Client(streamable_http_client(server_url, http_client=http_client)) as client:
            yield client


async def _with_session(
    server_url: str,
    headers: dict[str, str],
    fn: Callable[[Any], Coroutine[Any, Any, _T]],
) -> _T:
    open_session = _v2_session if _is_mcp_v2() else _v1_session
    async with open_session(server_url, headers) as session:
        return await fn(session)


def _run(ws: Any, server_url: str, fn: Callable[[Any], Coroutine[Any, Any, _T]]) -> _T:
    headers = _auth_headers(ws)
    require_gateway_ok(server_url, headers)
    return asyncio.run(_with_session(server_url, headers, fn))


async def _list_tools(session: Any) -> list[Any]:
    return list((await session.list_tools()).tools)


async def _call_tool(session: Any, *, name: str, arguments: dict[str, Any]) -> Any:
    return await session.call_tool(name, arguments)


def list_mcp_tools(ws: Any, server_url: str) -> list[Any]:
    """Return MCP tools from *server_url* using the workspace Bearer (OAuth or PAT)."""
    return _run(ws, server_url, _list_tools)


def call_mcp_tool(ws: Any, server_url: str, name: str, arguments: dict[str, Any]) -> Any:
    """Call one MCP tool on *server_url* using the workspace Bearer (OAuth or PAT)."""
    return _run(ws, server_url, partial(_call_tool, name=name, arguments=arguments))


__all__ = ["McpGatewayError", "call_mcp_tool", "list_mcp_tools", "require_gateway_ok"]
