"""Tests for the ``rest_mcp`` payload's typed ``spec`` contract.

These contracts are **payload-owned** (``payloads.rest_mcp.contracts``); the
framework knows only the generic ``PayloadFile`` envelope.  The surface is a
hard-cut 2.0 contract: every spec model is ``extra="forbid"`` so a misspelled
or misplaced key fails fast, and the fail-closed rails (https egress, non-GET
opt-in, resolvable template placeholders, confined code-tool paths) are
enforced at load time.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from payloads.rest_mcp.contracts import (
    ApiSpec,
    NoAuth,
    RestConfig,
    RestMcpPayloadFile,
    RestMcpPayloadSpec,
    StaticAuth,
    ToolRequest,
    ToolSpec,
)


def _get_tool(name: str = "get_order_status") -> dict[str, Any]:
    return {
        "name": name,
        "description": "Fetch order status.",
        "request": {
            "method": "GET",
            "path": "/orders/{order_id}/status",
            "args": [{"name": "order_id", "in": "path", "type": "string", "required": True}],
        },
        "response": {"project": ["status"]},
    }


def _api(name: str = "orders") -> dict[str, Any]:
    return {
        "name": name,
        "rest": {
            "base_url": "https://orders.internal.example.com",
            "allowed_hosts": ["orders.internal.example.com"],
            "auth": {"mode": "none"},
        },
        "tools": [_get_tool()],
    }


def _spec() -> dict[str, Any]:
    return {"apis": [_api()]}


class TestValidSpec:
    def test_minimal_valid_spec_round_trips(self) -> None:
        spec = RestMcpPayloadSpec.model_validate(_spec())
        assert spec.apis[0].name == "orders"
        assert isinstance(spec.apis[0].rest.auth, NoAuth)
        assert spec.apis[0].rest.defaults.timeout_s == 30
        assert spec.apis[0].rest.defaults.max_response_bytes == 100_000
        assert spec.apis[0].tools[0].request.args[0].location == "path"

    def test_full_envelope_round_trips(self) -> None:
        f = RestMcpPayloadFile.model_validate({"payload": {"adapter": "rest_mcp", "spec": _spec()}})
        assert f.payload.adapter == "rest_mcp"
        assert f.payload.spec.apis[0].tools[0].name == "get_order_status"


class TestExtraForbid:
    def test_misspelled_rest_key_rejected(self) -> None:
        api = _api()
        api["rest"]["base_urls"] = "typo"
        with pytest.raises(ValidationError):
            RestMcpPayloadSpec.model_validate({"apis": [api]})

    def test_unknown_top_level_spec_key_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RestMcpPayloadSpec.model_validate({"apis": [_api()], "extra": True})


class TestAuthUnion:
    def test_static_auth_parses(self) -> None:
        auth = StaticAuth.model_validate({"mode": "static", "secret_scope": "s", "secret_key": "k"})
        assert auth.header == "Authorization"
        assert auth.value_prefix == "Bearer "

    def test_static_auth_requires_secret_fields(self) -> None:
        with pytest.raises(ValidationError):
            RestConfig.model_validate(
                {
                    "base_url": "https://a.example.com",
                    "allowed_hosts": ["a.example.com"],
                    "auth": {"mode": "static"},
                }
            )

    def test_unknown_auth_mode_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RestConfig.model_validate(
                {
                    "base_url": "https://a.example.com",
                    "allowed_hosts": ["a.example.com"],
                    "auth": {"mode": "mtls"},
                }
            )


class TestEgressRails:
    def test_non_https_base_url_rejected(self) -> None:
        with pytest.raises(ValidationError, match="https"):
            RestConfig.model_validate({"base_url": "http://a.example.com", "allowed_hosts": ["a.example.com"]})

    def test_base_url_host_must_be_allow_listed(self) -> None:
        with pytest.raises(ValidationError, match="allowed_hosts"):
            RestConfig.model_validate({"base_url": "https://evil.example.com", "allowed_hosts": ["a.example.com"]})

    def test_empty_allow_list_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RestConfig.model_validate({"base_url": "https://a.example.com", "allowed_hosts": []})


class TestMutatingOptIn:
    def test_non_get_requires_mutating(self) -> None:
        with pytest.raises(ValidationError, match="mutating"):
            ToolSpec.model_validate(
                {
                    "name": "cancel",
                    "description": "Cancel.",
                    "request": {"method": "POST", "path": "/cancel"},
                }
            )

    def test_non_get_with_mutating_accepted(self) -> None:
        tool = ToolSpec.model_validate(
            {
                "name": "cancel",
                "description": "Cancel.",
                "mutating": True,
                "request": {"method": "POST", "path": "/cancel"},
            }
        )
        assert tool.mutating is True

    def test_get_cannot_be_mutating(self) -> None:
        with pytest.raises(ValidationError, match="GET"):
            ToolSpec.model_validate(
                {
                    "name": "list",
                    "description": "List.",
                    "mutating": True,
                    "request": {"method": "GET", "path": "/list"},
                }
            )


class TestBodyTemplate:
    def test_template_on_get_rejected(self) -> None:
        with pytest.raises(ValidationError, match="GET"):
            ToolRequest.model_validate({"method": "GET", "path": "/x", "body_template": '{"a": {{a}}}'})

    def test_unknown_placeholder_rejected(self) -> None:
        with pytest.raises(ValidationError, match="rows"):
            ToolRequest.model_validate(
                {
                    "method": "POST",
                    "path": "/invocations",
                    "args": [{"name": "cols", "in": "body", "type": "array"}],
                    "body_template": '{"dataframe_records": {{rows}}}',
                }
            )

    def test_placeholder_resolves_against_args(self) -> None:
        req = ToolRequest.model_validate(
            {
                "method": "POST",
                "path": "/invocations",
                "args": [{"name": "rows", "in": "body", "type": "array"}],
                "body_template": '{"dataframe_records": {{rows}}}',
            }
        )
        assert req.body_template is not None

    def test_placeholder_resolves_against_input_schema(self) -> None:
        req = ToolRequest.model_validate(
            {
                "method": "POST",
                "path": "/invocations",
                "input_schema": {"type": "object", "properties": {"rows": {"type": "array"}}},
                "body_template": '{"inputs": {{rows}}}',
            }
        )
        assert req.input_schema is not None


class TestPathArgs:
    def test_path_placeholder_must_be_declared(self) -> None:
        with pytest.raises(ValidationError, match="not declared"):
            ToolRequest.model_validate({"method": "GET", "path": "/orders/{order_id}"})

    def test_declared_path_arg_must_appear_in_path(self) -> None:
        with pytest.raises(ValidationError, match="not referenced"):
            ToolRequest.model_validate(
                {
                    "method": "GET",
                    "path": "/orders",
                    "args": [{"name": "order_id", "in": "path"}],
                }
            )

    def test_duplicate_arg_names_rejected(self) -> None:
        with pytest.raises(ValidationError, match="duplicate"):
            ToolRequest.model_validate(
                {
                    "method": "GET",
                    "path": "/x",
                    "args": [
                        {"name": "q", "in": "query"},
                        {"name": "q", "in": "query"},
                    ],
                }
            )


class TestCodeToolPathConfinement:
    def _api_with_code_module(self, module: str) -> dict[str, Any]:
        api = _api()
        api["tools"] = []
        api["code_tools"] = [{"module": module}]
        return api

    def test_valid_relative_module_accepted(self) -> None:
        api = ApiSpec.model_validate(self._api_with_code_module("tools/score.py"))
        assert api.code_tools[0].module == "tools/score.py"

    @pytest.mark.parametrize(
        "bad",
        ["/etc/passwd.py", "~/secrets.py", "tools/../../escape.py", "tools/score.txt"],
    )
    def test_unsafe_module_paths_rejected(self, bad: str) -> None:
        with pytest.raises(ValidationError):
            ApiSpec.model_validate(self._api_with_code_module(bad))


class TestUniquenessAndPresence:
    def test_api_requires_at_least_one_tool(self) -> None:
        api = _api()
        api["tools"] = []
        with pytest.raises(ValidationError, match="at least one"):
            ApiSpec.model_validate(api)

    def test_duplicate_tool_names_rejected(self) -> None:
        api = _api()
        api["tools"] = [_get_tool("dup"), _get_tool("dup")]
        with pytest.raises(ValidationError, match="duplicate tool"):
            ApiSpec.model_validate(api)

    def test_duplicate_api_names_rejected(self) -> None:
        with pytest.raises(ValidationError, match="duplicate"):
            RestMcpPayloadSpec.model_validate({"apis": [_api("orders"), _api("orders")]})

    def test_empty_apis_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RestMcpPayloadSpec.model_validate({"apis": []})

    def test_bad_api_name_slug_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ApiSpec.model_validate(_api("Orders API"))
