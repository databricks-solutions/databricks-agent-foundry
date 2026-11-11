"""Tests for the optional UC **MCP Service** governance layer (path C, Beta).

Exercises the SDK-thin logic against :class:`FakeWorkspaceClient`: the wire
shapes proven in the workspace spike (``connections/{fqn}`` reference,
``RATE_LIMIT_*`` / ``POLICY_TYPE_*`` enum mapping, ``tokens`` rate-limit field,
``EXECUTE`` grants), create-if-absent + governance reconcile idempotency, schema
ensure, workspace-state round-trip, teardown, and graceful Beta-preview-off.
"""

from __future__ import annotations

from typing import Any

from payloads.rest_mcp.contracts import McpServiceSpec, RestMcpPayloadSpec
from payloads.rest_mcp.deploy.mcp_service import (
    build_rate_limits,
    build_service_policies,
    default_schema,
    destroy_mcp_services,
    loaded_mcp_services,
    mcp_service_full_name,
    provision_mcp_services,
)

_SERVICES_API = "/api/2.1/unity-catalog/mcp-services"

_SERVICE_BLOCK = {
    "include_tool_selectors": ["get_*", "search_repositories"],
    "service_policies": [
        {"name": "block", "policy_type": "builtin", "handler": "disallow_writes", "options": {"k": "v"}},
        {"name": "cust", "policy_type": "custom", "handler": "cat.sch.fn"},
    ],
    "rate_limits": [{"key": "user", "principal": "a@b.com", "calls": 100}],
    "grants": {"execute": ["group:analysts", "user@x.com"]},
}


def _api(name: str, *, service: dict[str, Any] | None = None) -> dict[str, Any]:
    api: dict[str, Any] = {
        "name": name,
        "rest": {
            "base_url": f"https://{name}.internal.example.com",
            "allowed_hosts": [f"{name}.internal.example.com"],
            "auth": {"mode": "none"},
        },
        "tools": [
            {
                "name": "get_thing",
                "description": "Fetch a thing.",
                "request": {"method": "GET", "path": "/thing", "args": []},
            }
        ],
    }
    if service is not None:
        api["mcp_service"] = service
    return api


def _spec(*apis: dict[str, Any]) -> RestMcpPayloadSpec:
    return RestMcpPayloadSpec.model_validate({"apis": list(apis or (_api("orders"),))})


def _service_creates(ws: Any) -> list[dict[str, Any]]:
    return [b for m, p, b in ws.api_client.calls if m == "POST" and p == _SERVICES_API and b is not None]


# Shared provisioning kwargs (instance / target / derived catalog + schema).
_PROV: dict[str, Any] = {"instance_name": "demo", "target": "dev", "catalog": "cat", "schema_prefix": "pfx"}
_CONN = {"github": "demo_github_mcp"}


class TestPureHelpers:
    def test_full_name_and_default_schema(self) -> None:
        assert mcp_service_full_name("cat", "sch", "github") == "cat.sch.github"
        assert default_schema("my_proxy") == "my_proxy_rest_mcp"

    def test_build_rate_limits_maps_to_wire_tokens_and_enums(self) -> None:
        svc = McpServiceSpec.model_validate(
            {"rate_limits": [{"key": "service_principal", "principal": "app-123", "calls": 42}]}
        )
        assert build_rate_limits(svc) == [
            {
                "key": "RATE_LIMIT_KEY_SERVICE_PRINCIPAL",
                "principal": "app-123",
                "renewal_period": "RATE_LIMIT_RENEWAL_PERIOD_MINUTE",
                "tokens": 42,
            }
        ]

    def test_build_service_policies_wraps_custom_handler(self) -> None:
        svc = McpServiceSpec.model_validate(
            {
                "service_policies": [
                    {"name": "b", "policy_type": "builtin", "handler": "disallow_writes"},
                    {"name": "c", "policy_type": "custom", "handler": "cat.sch.fn", "options": {"x": "y"}},
                ]
            }
        )
        assert build_service_policies(svc) == [
            {"name": "b", "policy_type": "POLICY_TYPE_BUILTIN", "handler": "disallow_writes"},
            {
                "name": "c",
                "policy_type": "POLICY_TYPE_CUSTOM",
                "handler": "functions/cat.sch.fn",
                "options": {"x": "y"},
            },
        ]


class TestProvision:
    def test_creates_service_from_connection_and_reconciles(self, fake_ws: Any) -> None:
        ws = fake_ws()
        spec = _spec(_api("github", service=_SERVICE_BLOCK))
        provisioned = provision_mcp_services(ws, spec, _CONN, **_PROV)
        assert provisioned == ["cat.pfx_rest_mcp.github"]
        # Schema ensured, service created from the connection.
        assert "cat.pfx_rest_mcp" in ws.api_client.schemas
        creates = _service_creates(ws)
        assert len(creates) == 1
        assert creates[0]["config"]["connection"]["name"] == "connections/demo_github_mcp"
        assert creates[0]["config"]["include_tool_selectors"] == ["get_*", "search_repositories"]

    def test_reconcile_patches_selectors_policies_and_rate_limits(self, fake_ws: Any) -> None:
        ws = fake_ws()
        provision_mcp_services(ws, _spec(_api("github", service=_SERVICE_BLOCK)), _CONN, **_PROV)
        patched_fields = {next(iter(body["config"])) for _path, body in ws.service_patches() if body is not None}
        assert patched_fields == {"include_tool_selectors", "service_policies", "rate_limits"}
        stored = ws.api_client.services["cat.pfx_rest_mcp.github"]["config"]
        assert stored["rate_limits"][0]["tokens"] == 100
        assert stored["service_policies"][1]["handler"] == "functions/cat.sch.fn"

    def test_grants_execute_to_listed_principals(self, fake_ws: Any) -> None:
        ws = fake_ws()
        provision_mcp_services(ws, _spec(_api("github", service=_SERVICE_BLOCK)), _CONN, **_PROV)
        assert ws.api_client.grants == [
            (
                "cat.pfx_rest_mcp.github",
                {
                    "changes": [
                        {"principal": "group:analysts", "add": ["EXECUTE"]},
                        {"principal": "user@x.com", "add": ["EXECUTE"]},
                    ]
                },
            )
        ]

    def test_idempotent_second_deploy_does_not_recreate(self, fake_ws: Any) -> None:
        ws = fake_ws()
        spec = _spec(_api("github", service=_SERVICE_BLOCK))
        provision_mcp_services(ws, spec, _CONN, **_PROV)
        provision_mcp_services(ws, spec, _CONN, **_PROV)
        assert len(_service_creates(ws)) == 1  # created once; reconciled twice

    def test_per_api_catalog_schema_override(self, fake_ws: Any) -> None:
        ws = fake_ws()
        block = {**_SERVICE_BLOCK, "catalog": "other_cat", "schema": "other_sch"}
        provisioned = provision_mcp_services(ws, _spec(_api("github", service=block)), _CONN, **_PROV)
        assert provisioned == ["other_cat.other_sch.github"]

    def test_skips_api_without_service_or_disabled(self, fake_ws: Any) -> None:
        ws = fake_ws()
        disabled = {**_SERVICE_BLOCK, "enabled": False}
        spec = _spec(_api("plain"), _api("github", service=disabled))
        assert provision_mcp_services(ws, spec, {}, **_PROV) == []
        assert _service_creates(ws) == []

    def test_skips_when_connection_missing(self, fake_ws: Any) -> None:
        ws = fake_ws()
        # No connection for github -> the service is skipped (not built from nothing).
        assert provision_mcp_services(ws, _spec(_api("github", service=_SERVICE_BLOCK)), {}, **_PROV) == []

    def test_preview_off_degrades_without_crashing(self, fake_ws: Any) -> None:
        ws = fake_ws(service_preview_off=True)
        spec = _spec(_api("github", service=_SERVICE_BLOCK))
        assert provision_mcp_services(ws, spec, _CONN, **_PROV) == []  # skipped, no state saved
        assert loaded_mcp_services(ws, spec, instance_name="demo", target="dev") == {}

    def test_dry_run_and_offline_write_nothing(self, fake_ws: Any) -> None:
        spec = _spec(_api("github", service=_SERVICE_BLOCK))
        ws = fake_ws()
        assert provision_mcp_services(ws, spec, _CONN, dry_run=True, **_PROV) == ["cat.pfx_rest_mcp.github"]
        assert ws.api_client.calls == []
        assert provision_mcp_services(None, spec, {}, **_PROV) == ["cat.pfx_rest_mcp.github"]


class TestStateAndDestroy:
    def test_state_roundtrips_and_destroy_clears(self, fake_ws: Any) -> None:
        ws = fake_ws()
        spec = _spec(_api("github", service=_SERVICE_BLOCK))
        provision_mcp_services(ws, spec, _CONN, **_PROV)
        loaded = loaded_mcp_services(ws, spec, instance_name="demo", target="dev")
        assert loaded == {"github": "cat.pfx_rest_mcp.github"}

        removed = destroy_mcp_services(ws, spec, instance_name="demo", target="dev")
        assert removed == ["cat.pfx_rest_mcp.github"]
        assert "cat.pfx_rest_mcp.github" not in ws.api_client.services
        assert loaded_mcp_services(ws, spec, instance_name="demo", target="dev") == {}

    def test_destroy_is_noop_when_never_enabled(self, fake_ws: Any) -> None:
        ws = fake_ws()
        assert destroy_mcp_services(ws, _spec(_api("plain")), instance_name="demo", target="dev") == []

    def test_destroy_dry_run_deletes_nothing(self, fake_ws: Any) -> None:
        ws = fake_ws()
        spec = _spec(_api("github", service=_SERVICE_BLOCK))
        provision_mcp_services(ws, spec, _CONN, **_PROV)
        removed = destroy_mcp_services(ws, spec, instance_name="demo", target="dev", dry_run=True)
        assert removed == ["cat.pfx_rest_mcp.github"]
        assert "cat.pfx_rest_mcp.github" in ws.api_client.services  # not actually deleted
