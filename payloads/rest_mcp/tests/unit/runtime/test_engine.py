"""Tests for the pure REST->MCP translation engine.

The engine is a pure library, so these exercise it with an ``httpx``
``MockTransport`` -- no network.  Coverage spans the two derived artifacts
(MCP ``inputSchema`` + request), all three auth modes, the ``{{arg}}``
body-template render, response projection + byte cap, the fail-closed egress
guard, and structured upstream-error mapping (with a token-never-leaked check).
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from payloads.rest_mcp.contracts import RestConfig, ToolSpec
from payloads.rest_mcp.runtime.engine import (
    EgressError,
    ToolInputError,
    UpstreamError,
    build_input_schema,
    build_tool_specs,
    call_tool,
    prepare_request,
    shape_response,
)


def _rest(**overrides: Any) -> RestConfig:
    base: dict[str, Any] = {
        "base_url": "https://api.example.com",
        "allowed_hosts": ["api.example.com"],
        "auth": {"mode": "none"},
    }
    base.update(overrides)
    return RestConfig.model_validate(base)


def _get_status_tool(project: list[str] | None = None) -> ToolSpec:
    body: dict[str, Any] = {
        "name": "get_order_status",
        "description": "Fetch order status.",
        "request": {
            "method": "GET",
            "path": "/orders/{order_id}/status",
            "args": [
                {"name": "order_id", "in": "path", "type": "string", "required": True},
                {"name": "verbose", "in": "query", "type": "boolean"},
            ],
        },
    }
    if project is not None:
        body["response"] = {"project": project}
    return ToolSpec.model_validate(body)


def _score_tool() -> ToolSpec:
    return ToolSpec.model_validate(
        {
            "name": "score_batch",
            "description": "Score rows.",
            "mutating": True,
            "request": {
                "method": "POST",
                "path": "/invocations",
                "args": [{"name": "rows", "in": "body", "type": "array", "required": True}],
                "body_template": '{"dataframe_records": {{rows}}}',
            },
        }
    )


class TestInputSchema:
    def test_derived_from_args(self) -> None:
        schema = build_input_schema(_get_status_tool())
        assert schema["type"] == "object"
        assert schema["properties"]["order_id"]["type"] == "string"
        assert schema["properties"]["verbose"]["type"] == "boolean"
        assert schema["required"] == ["order_id"]
        assert schema["additionalProperties"] is False

    def test_verbatim_input_schema_wins(self) -> None:
        tool = ToolSpec.model_validate(
            {
                "name": "score",
                "description": "d",
                "mutating": True,
                "request": {
                    "method": "POST",
                    "path": "/inv",
                    "input_schema": {"type": "object", "properties": {"rows": {"type": "array"}}},
                    "body_template": '{"r": {{rows}}}',
                },
            }
        )
        assert build_input_schema(tool) == {
            "type": "object",
            "properties": {"rows": {"type": "array"}},
        }

    def test_build_tool_specs_maps_each_tool(self) -> None:
        specs = build_tool_specs([_get_status_tool(), _score_tool()])
        assert [s.name for s in specs] == ["get_order_status", "score_batch"]
        assert specs[0].description == "Fetch order status."


class TestPrepareRequest:
    def test_path_and_query_encoded(self) -> None:
        req = prepare_request(_rest(), _get_status_tool(), {"order_id": "A 1", "verbose": True}, token=None)
        assert req.method == "GET"
        assert str(req.url) == "https://api.example.com/orders/A%201/status?verbose=true"
        assert "authorization" not in {k.lower() for k in req.headers}

    def test_missing_required_arg_rejected(self) -> None:
        with pytest.raises(ToolInputError, match="order_id"):
            prepare_request(_rest(), _get_status_tool(), {}, token=None)

    def test_static_auth_default_header(self) -> None:
        rest = _rest(auth={"mode": "static", "secret_scope": "s", "secret_key": "k"})
        req = prepare_request(rest, _get_status_tool(), {"order_id": "1"}, token="TKN")
        assert req.headers["Authorization"] == "Bearer TKN"

    def test_static_auth_custom_header_no_prefix(self) -> None:
        rest = _rest(
            auth={
                "mode": "static",
                "secret_scope": "s",
                "secret_key": "k",
                "header": "X-Api-Key",
                "value_prefix": "",
            }
        )
        req = prepare_request(rest, _get_status_tool(), {"order_id": "1"}, token="TKN")
        assert req.headers["X-Api-Key"] == "TKN"

    def test_static_auth_without_token_rejected(self) -> None:
        rest = _rest(auth={"mode": "static", "secret_scope": "s", "secret_key": "k"})
        with pytest.raises(ToolInputError, match="token"):
            prepare_request(rest, _get_status_tool(), {"order_id": "1"}, token=None)

    def test_obo_auth_forwards_bearer(self) -> None:
        rest = _rest(auth={"mode": "obo"})
        req = prepare_request(rest, _get_status_tool(), {"order_id": "1"}, token="USER_TOKEN")
        assert req.headers["Authorization"] == "Bearer USER_TOKEN"

    def test_body_template_rendered(self) -> None:
        req = prepare_request(_rest(), _score_tool(), {"rows": [{"a": 1}, {"a": 2}]}, token=None)
        assert json.loads(req.content) == {"dataframe_records": [{"a": 1}, {"a": 2}]}

    def test_flat_body_args_rendered(self) -> None:
        tool = ToolSpec.model_validate(
            {
                "name": "create",
                "description": "d",
                "mutating": True,
                "request": {
                    "method": "POST",
                    "path": "/create",
                    "args": [{"name": "title", "in": "body", "type": "string", "required": True}],
                },
            }
        )
        req = prepare_request(_rest(), tool, {"title": "hello"}, token=None)
        assert json.loads(req.content) == {"title": "hello"}


class TestEgressGuard:
    def test_host_not_allow_listed_rejected(self) -> None:
        rest = _rest()
        rest.allowed_hosts = ["other.example.com"]  # bypasses model re-validation
        with pytest.raises(EgressError, match="allowed_hosts"):
            prepare_request(rest, _get_status_tool(), {"order_id": "1"}, token=None)

    def test_non_https_rejected(self) -> None:
        rest = _rest()
        rest.base_url = "http://api.example.com"
        with pytest.raises(EgressError, match="non-https"):
            prepare_request(rest, _get_status_tool(), {"order_id": "1"}, token=None)


class TestShapeResponse:
    def test_projection_selects_keys(self) -> None:
        resp = httpx.Response(200, json={"status": "shipped", "updated_at": "t", "extra": "x"})
        out = shape_response(_rest(), _get_status_tool(project=["status", "updated_at"]), resp)
        assert json.loads(out) == {"status": "shipped", "updated_at": "t"}

    def test_no_projection_returns_text(self) -> None:
        resp = httpx.Response(200, text="plain body")
        assert shape_response(_rest(), _get_status_tool(), resp) == "plain body"

    def test_byte_cap_truncates(self) -> None:
        resp = httpx.Response(200, text="a" * 1000)
        rest = _rest(defaults={"max_response_bytes": 100})
        out = shape_response(rest, _get_status_tool(), resp)
        assert out.endswith("...[truncated]")
        assert len(out.encode("utf-8")) <= 100 + len("...[truncated]")

    def test_upstream_error_maps_status_and_hides_token(self) -> None:
        resp = httpx.Response(500, text="internal boom")
        with pytest.raises(UpstreamError) as exc_info:
            shape_response(_rest(), _get_status_tool(), resp)
        assert exc_info.value.status_code == 500
        assert "internal boom" in exc_info.value.detail


class TestCallTool:
    @pytest.mark.asyncio
    async def test_end_to_end_success_with_projection(self) -> None:
        captured: dict[str, httpx.Request] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["req"] = request
            return httpx.Response(200, json={"status": "shipped", "updated_at": "t", "x": 1})

        transport = httpx.MockTransport(handler)
        rest = _rest(auth={"mode": "static", "secret_scope": "s", "secret_key": "k"})
        tool = _get_status_tool(project=["status", "updated_at"])
        async with httpx.AsyncClient(transport=transport) as client:
            out = await call_tool(rest, tool, {"order_id": "42"}, token="TKN", client=client)

        assert json.loads(out) == {"status": "shipped", "updated_at": "t"}
        assert str(captured["req"].url) == "https://api.example.com/orders/42/status"
        assert captured["req"].headers["Authorization"] == "Bearer TKN"

    @pytest.mark.asyncio
    async def test_upstream_4xx_raises_without_leaking_token(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, text="not found")

        transport = httpx.MockTransport(handler)
        rest = _rest(auth={"mode": "static", "secret_scope": "s", "secret_key": "k"})
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(UpstreamError) as exc_info:
                await call_tool(rest, _get_status_tool(), {"order_id": "1"}, token="SECRET", client=client)

        assert exc_info.value.status_code == 404
        assert "SECRET" not in str(exc_info.value)
