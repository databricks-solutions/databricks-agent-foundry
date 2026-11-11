"""Tests for the MCP registration core.

``collect_api_tools`` turns a spec into MCP cards + dispatch tables (rejecting
name collisions), and ``dispatch_tool_call`` routes to the engine or a code
tool -- both driven over an ``httpx.MockTransport`` with an injected token
provider, so no network or Databricks SDK is involved.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp.server.lowlevel import Server

from payloads.rest_mcp.contracts import ApiSpec
from payloads.rest_mcp.runtime.server.registry import (
    build_api_server,
    collect_api_tools,
    dispatch_tool_call,
)

_STATUS_TOOL: dict[str, Any] = {
    "name": "get_status",
    "description": "Get order status.",
    "request": {"method": "GET", "path": "/status"},
}

_CODE_MODULE = """
from payloads.rest_mcp.runtime import tool, HttpClient


@tool(name="score", description="Score a value.")
async def score(n: int, *, http: HttpClient) -> dict:
    return await http.post_json("/score", {"n": n})
"""

_CODE_MODULE_COLLIDING = """
from payloads.rest_mcp.runtime import tool, HttpClient


@tool(name="get_status", description="Collides with the declarative tool.")
async def get_status(*, http: HttpClient) -> dict:
    return {}
"""


def _api(**overrides: Any) -> ApiSpec:
    base: dict[str, Any] = {
        "name": "orders",
        "rest": {
            "base_url": "https://api.example.com",
            "allowed_hosts": ["api.example.com"],
            "auth": {"mode": "none"},
        },
        "tools": [],
        "code_tools": [],
    }
    base.update(overrides)
    return ApiSpec.model_validate(base)


def _static_api(**overrides: Any) -> ApiSpec:
    rest: dict[str, Any] = {
        "base_url": "https://api.example.com",
        "allowed_hosts": ["api.example.com"],
        "auth": {"mode": "static", "secret_scope": "s", "secret_key": "k"},
    }
    return _api(rest=rest, **overrides)


def _write_module(root: Path, rel: str, body: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class TestCollectApiTools:
    def test_declarative_and_code_cards(self, tmp_path: Path) -> None:
        _write_module(tmp_path, "tools/score.py", _CODE_MODULE)
        api = _api(tools=[_STATUS_TOOL], code_tools=[{"module": "tools/score.py"}])
        tools = collect_api_tools(api, tmp_path)

        assert {d.name for d in tools.descriptors} == {"get_status", "score"}
        assert set(tools.declarative) == {"get_status"}
        assert set(tools.code) == {"score"}
        status_card = next(d for d in tools.descriptors if d.name == "get_status")
        assert status_card.inputSchema["type"] == "object"

    def test_name_collision_rejected(self, tmp_path: Path) -> None:
        _write_module(tmp_path, "tools/dup.py", _CODE_MODULE_COLLIDING)
        api = _api(tools=[_STATUS_TOOL], code_tools=[{"module": "tools/dup.py"}])
        with pytest.raises(ValueError, match="duplicate tool name"):
            collect_api_tools(api, tmp_path)


class TestDispatch:
    @pytest.mark.asyncio
    async def test_declarative_call(self, tmp_path: Path) -> None:
        api = _api(tools=[_STATUS_TOOL])
        tools = collect_api_tools(api, tmp_path)
        async with _client(lambda r: httpx.Response(200, json={"status": "ok"})) as client:
            out = await dispatch_tool_call(api, tools, "get_status", {}, client=client, token_provider=lambda: None)
        assert "ok" in out

    @pytest.mark.asyncio
    async def test_token_provider_reaches_upstream(self, tmp_path: Path) -> None:
        api = _static_api(tools=[_STATUS_TOOL])
        tools = collect_api_tools(api, tmp_path)
        seen: dict[str, httpx.Request] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["req"] = request
            return httpx.Response(200, json={"status": "ok"})

        async with _client(handler) as client:
            await dispatch_tool_call(api, tools, "get_status", {}, client=client, token_provider=lambda: "TKN")

        assert seen["req"].headers["Authorization"] == "Bearer TKN"

    @pytest.mark.asyncio
    async def test_code_tool_call(self, tmp_path: Path) -> None:
        _write_module(tmp_path, "tools/score.py", _CODE_MODULE)
        api = _api(code_tools=[{"module": "tools/score.py"}])
        tools = collect_api_tools(api, tmp_path)
        async with _client(lambda r: httpx.Response(200, json={"scored": True})) as client:
            out = await dispatch_tool_call(api, tools, "score", {"n": 5}, client=client, token_provider=lambda: None)
        assert "scored" in out

    @pytest.mark.asyncio
    async def test_unknown_tool_raises(self, tmp_path: Path) -> None:
        api = _api(tools=[_STATUS_TOOL])
        tools = collect_api_tools(api, tmp_path)
        async with httpx.AsyncClient() as client:
            with pytest.raises(ValueError, match="unknown tool"):
                await dispatch_tool_call(api, tools, "nope", {}, client=client, token_provider=lambda: None)


@pytest.mark.asyncio
async def test_build_api_server_returns_server(tmp_path: Path) -> None:
    api = _api(tools=[_STATUS_TOOL])
    async with httpx.AsyncClient() as client:
        server = build_api_server(api, tmp_path, client=client, token_provider=lambda: None)
    assert isinstance(server, Server)
    assert server.name == "orders"
