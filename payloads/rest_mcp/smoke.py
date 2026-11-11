"""Smoke probes for the rest_mcp payload: each API's tools through its UC MCP gateway."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from agent_foundry.smoke import SmokeFailure, SmokeProbe, SmokeTarget

if TYPE_CHECKING:
    from payloads.rest_mcp.contracts import ApiSpec


def rest_mcp_smoke_probes(target: SmokeTarget) -> list[SmokeProbe]:
    from payloads.rest_mcp.runtime.config import load_rest_mcp_spec

    apis = load_rest_mcp_spec(target.instance_dir).apis
    return [SmokeProbe(f"mcp gateway {api.name}", partial(gateway_lists_tools, target, api)) for api in apis]


def gateway_lists_tools(target: SmokeTarget, api: ApiSpec) -> str:
    from agent_foundry.testing.mcp_session import list_mcp_tools
    from payloads.rest_mcp.deploy.connection import connection_name

    connection = connection_name(target.instance_dir.name, api.name)
    url = f"{target.workspace_host}/api/2.0/mcp/external/{connection}"
    listed = {tool.name for tool in list_mcp_tools(target.ws, url)}
    missing = sorted({tool.name for tool in api.tools} - listed)
    if missing:
        raise SmokeFailure(f"{connection} does not list declared tools {missing}")
    return ", ".join(sorted(listed))


__all__ = ["gateway_lists_tools", "rest_mcp_smoke_probes"]
