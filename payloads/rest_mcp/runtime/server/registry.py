"""Wire one API's tools onto a low-level MCP ``Server`` (FastMCP-free).

A ``rest_mcp`` API exposes two kinds of tools on the same MCP surface:

* **Declarative** tools (Tier 1-2) -- driven by the pure engine; their
  ``inputSchema`` is derived from the spec, not a Python signature.
* **Code** tools (Tier 3) -- discovered from instance modules; they run their
  own body against the injected :class:`HttpClient`.

FastMCP's ``add_tool`` derives schemas from function signatures, which does not
fit config-derived tools, so we use the low-level :class:`mcp.server.Server`
and answer ``list_tools`` / ``call_tool`` ourselves with explicit schemas and
dynamic dispatch.  This module stays ASGI-free: the host slice mounts the
returned server over Streamable HTTP.

The per-request hop-2 token is supplied through an injected ``token_provider``
seam -- for ``static`` auth it returns a pre-resolved secret, for ``obo`` the
per-request forwarded token, for ``none`` it returns ``None`` -- so this core
never touches the Databricks SDK or request context and stays unit-testable.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import mcp.types as mcp_types
from mcp.server.lowlevel import Server

from payloads.rest_mcp.contracts import ApiSpec, ToolSpec
from payloads.rest_mcp.runtime.code_tools import CodeTool, discover_code_tools
from payloads.rest_mcp.runtime.engine import build_tool_specs, call_tool, cap_text
from payloads.rest_mcp.runtime.http_client import HttpClient

# Returns the hop-2 token for the current request (``None`` for ``none`` auth).
TokenProvider = Callable[[], str | None]


@dataclass(frozen=True)
class ApiTools:
    """The resolved tool set for one API: MCP cards plus dispatch tables."""

    descriptors: list[mcp_types.Tool]
    declarative: dict[str, ToolSpec]
    code: dict[str, CodeTool]


def collect_api_tools(api: ApiSpec, instance_root: Path) -> ApiTools:
    """Resolve an API's declarative + code tools into cards and dispatch maps.

    Fails closed on a name collision -- across declarative tools, across code
    modules, or between the two -- so a call can never be ambiguous.
    """
    declarative = {tool.name: tool for tool in api.tools}
    descriptors: list[mcp_types.Tool] = [
        mcp_types.Tool(name=d.name, description=d.description, inputSchema=d.input_schema)
        for d in build_tool_specs(list(api.tools))
    ]

    code: dict[str, CodeTool] = {}
    for code_tool_spec in api.code_tools:
        for code_tool in discover_code_tools(instance_root, code_tool_spec.module):
            if code_tool.name in declarative or code_tool.name in code:
                raise ValueError(f"api {api.name!r}: duplicate tool name {code_tool.name!r}")
            code[code_tool.name] = code_tool
            descriptors.append(
                mcp_types.Tool(
                    name=code_tool.name,
                    description=code_tool.description,
                    inputSchema=code_tool.input_schema,
                )
            )

    return ApiTools(descriptors=descriptors, declarative=declarative, code=code)


def build_api_server(
    api: ApiSpec,
    instance_root: Path,
    *,
    client: httpx.AsyncClient,
    token_provider: TokenProvider,
) -> Server:
    """Build a low-level MCP ``Server`` exposing one API's tools.

    ``list_tools`` returns the pre-built cards; ``call_tool`` dispatches to the
    engine (declarative) or the code-tool body (Tier 3), both bound to the
    API's rails via ``api.rest`` + the injected ``token_provider`` / ``client``.
    """
    tools = collect_api_tools(api, instance_root)
    server: Server = Server(api.name)

    # The low-level ``Server`` decorators are untyped in the mcp SDK; the
    # handler bodies below are fully typed and covered by tests.
    @server.list_tools()  # type: ignore[no-untyped-call, untyped-decorator]
    async def _list_tools() -> list[mcp_types.Tool]:
        return tools.descriptors

    @server.call_tool()  # type: ignore[untyped-decorator]
    async def _call_tool(name: str, arguments: dict[str, Any]) -> list[mcp_types.ContentBlock]:
        text = await dispatch_tool_call(api, tools, name, arguments, client=client, token_provider=token_provider)
        return [mcp_types.TextContent(type="text", text=text)]

    return server


async def dispatch_tool_call(
    api: ApiSpec,
    tools: ApiTools,
    name: str,
    arguments: dict[str, Any],
    *,
    client: httpx.AsyncClient,
    token_provider: TokenProvider,
) -> str:
    """Route one MCP call to the engine (declarative) or a code tool (Tier 3).

    Returns the MCP text payload; raises ``ValueError`` for an unknown tool and
    propagates the engine's typed errors (``ToolInputError`` / ``EgressError``
    / ``UpstreamError``) so the server layer surfaces them as MCP errors.
    """
    declarative_tool = tools.declarative.get(name)
    if declarative_tool is not None:
        return await call_tool(api.rest, declarative_tool, arguments, token=token_provider(), client=client)

    code_tool = tools.code.get(name)
    if code_tool is not None:
        http = HttpClient(api.rest, token=token_provider(), client=client)
        result = await code_tool.func(**arguments, http=http)
        return _result_to_text(result, api.rest.defaults.max_response_bytes)

    raise ValueError(f"api {api.name!r}: unknown tool {name!r}")


def _result_to_text(result: Any, max_bytes: int) -> str:
    text = result if isinstance(result, str) else json.dumps(result, default=str)
    return cap_text(text, max_bytes)


__all__ = ["ApiTools", "TokenProvider", "build_api_server", "collect_api_tools", "dispatch_tool_call"]
