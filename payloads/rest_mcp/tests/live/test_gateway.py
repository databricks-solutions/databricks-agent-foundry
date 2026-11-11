"""Each API in payload.yml is served through its UC connection's MCP gateway with its declared tools."""

from __future__ import annotations

from typing import Any

from agent_foundry.testing.live import LiveTarget, published_outputs
from agent_foundry.testing.mcp_session import list_mcp_tools
from agent_foundry.testing.workspace import workspace_api_url
from payloads.rest_mcp.contracts import ApiSpec
from payloads.rest_mcp.runtime.config import load_rest_mcp_spec


def _missing_tools(ws: Any, target: LiveTarget, api: ApiSpec, connection: str) -> set[str]:
    url = workspace_api_url(target.binding, f"/api/2.0/mcp/external/{connection}")
    listed = {tool.name for tool in list_mcp_tools(ws, url)}
    return {tool.name for tool in api.tools} - listed


def test_gateway_lists_declared_tools(live_ws: Any, live_target: LiveTarget, live_variables: dict[str, str]) -> None:
    connections = published_outputs(live_variables, "rest_mcp_connection")
    apis = load_rest_mcp_spec(live_target.instance_dir).apis
    unpublished = sorted(api.name for api in apis if api.name not in connections)
    assert not unpublished, f"no UC connection published for: {unpublished}"
    missing = {api.name: _missing_tools(live_ws, live_target, api, connections[api.name]) for api in apis}
    assert not any(missing.values()), f"gateway does not list declared tools: {missing}"
