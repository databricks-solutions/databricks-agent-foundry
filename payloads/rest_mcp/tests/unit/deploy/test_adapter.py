"""Tests for the ``rest_mcp`` deploy adapter and its neutral spec builders.

Covers the framework seam (issue #326): the adapter validates the payload
spec up front, produces the lean ``AppPayloadSpec`` (uvicorn Streamable-HTTP
host + ``rest_mcp`` wheel extra) and a minimal ``AppInfraSpec``, and drives the
governed UC ``is_mcp`` connection lifecycle -- ``provision`` stashes the
context, ``after_reconcile`` creates the connection once the App URL is known,
and ``plan_destroy`` / ``destroy`` tear it down.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from agent_foundry.deploy.app_host import AppInfraSpec, AppPayloadSpec
from payloads.rest_mcp.adapter import RestMcpPayloadAdapter
from payloads.rest_mcp.deploy.spec import build_rest_mcp_infra, build_rest_mcp_payload_spec


def _spec() -> dict[str, Any]:
    return {
        "apis": [
            {
                "name": "orders",
                "rest": {
                    "base_url": "https://orders.internal.example.com",
                    "allowed_hosts": ["orders.internal.example.com"],
                    "auth": {"mode": "none"},
                },
                "tools": [
                    {
                        "name": "get_order_status",
                        "description": "Fetch order status.",
                        "request": {
                            "method": "GET",
                            "path": "/orders/{order_id}/status",
                            "args": [{"name": "order_id", "in": "path", "type": "string", "required": True}],
                        },
                        "response": {"project": ["status"]},
                    }
                ],
            }
        ]
    }


def _write_instance(tmp_path: Path, spec: dict[str, Any]) -> Path:
    inst = tmp_path / "my_proxy"
    payload_dir = inst / "payloads" / "rest_mcp"
    payload_dir.mkdir(parents=True)
    (payload_dir / "payload.yml").write_text(yaml.dump({"payload": {"adapter": "rest_mcp", "spec": spec}}))
    return inst


class TestBuildPayloadSpec:
    def test_lean_runtime_contract(self, tmp_path: Path) -> None:
        inst = _write_instance(tmp_path, _spec())
        payload = RestMcpPayloadAdapter().build_payload_spec(inst, {}, _spec())
        assert isinstance(payload, AppPayloadSpec)
        assert payload.command == ["python", "-m", "payloads.rest_mcp.runtime.start_server"]
        assert payload.requirements_extras == ("rest_mcp",)
        env = {e["name"]: e["value"] for e in payload.env}
        assert env["AGENT_FOUNDRY_PROJECT_ROOT"] == "."
        assert env["INSTANCE_DIR"] == "instances/my_proxy"

    def test_placeholder_instance_dir_when_hermetic(self) -> None:
        # No on-disk instance (dry-run): the host substitutes the var later.
        payload = build_rest_mcp_payload_spec(None, {})
        env = {e["name"]: e["value"] for e in payload.env}
        assert env["INSTANCE_DIR"] == "instances/${var.instance_dir_name}"

    def test_validates_spec_and_fails_closed(self, tmp_path: Path) -> None:
        inst = _write_instance(tmp_path, {"apis": []})  # apis must be non-empty
        with pytest.raises(ValidationError):
            RestMcpPayloadAdapter().build_payload_spec(inst, {}, {"apis": []})


class TestBuildInfra:
    def test_minimal_infra(self, tmp_path: Path) -> None:
        inst = _write_instance(tmp_path, _spec())
        infra = RestMcpPayloadAdapter().build_infra(inst, {}, target="dev")
        assert isinstance(infra, AppInfraSpec)
        assert "my_proxy" in infra.description
        assert infra.app_resources == []
        assert infra.sp_grants == []
        assert infra.compute_size == "medium"

    def test_infra_without_instance(self) -> None:
        infra = build_rest_mcp_infra(None)
        assert infra.description == "rest_mcp REST->MCP proxy (rest_mcp)"


class TestSelectionAndScaffolds:
    def test_applies_to_gates_on_payload_surface(self, tmp_path: Path) -> None:
        adapter = RestMcpPayloadAdapter()
        assert adapter.applies_to(tmp_path, {}) is False
        (tmp_path / "payloads" / "rest_mcp").mkdir(parents=True)
        assert adapter.applies_to(tmp_path, {}) is True

    def test_declares_rest_mcp_scaffold(self) -> None:
        declarations = RestMcpPayloadAdapter().scaffolds()

        assert len(declarations) == 1
        assert declarations[0].name == "rest_mcp"


class TestProvisionStashesContext:
    def test_provision_hands_context_to_after_reconcile(self, tmp_path: Path) -> None:
        from agent_foundry.bricks import DeployContext

        inst = _write_instance(tmp_path, _spec())
        ctx = DeployContext(instance_dir=inst, target="dev", variables={}, dry_run=True, config=None)
        assert RestMcpPayloadAdapter().provision(ctx) == 0
        assert ctx.variables["rest_mcp__instance_dir"] == str(inst)
        assert ctx.variables["rest_mcp__target"] == "dev"


class TestAfterReconcile:
    def _stash(self, inst: Path) -> dict[str, str]:
        return {"rest_mcp__instance_dir": str(inst), "rest_mcp__target": "dev"}

    def test_creates_connection_once_app_url_known(self, tmp_path: Path, fake_ws: Any) -> None:
        inst = _write_instance(tmp_path, _spec())
        ws = fake_ws()
        RestMcpPayloadAdapter().after_reconcile(ws, "my_proxy", self._stash(inst), dry_run=False)
        assert [p["name"] for p in ws.posts()] == ["my_proxy_orders_mcp"]
        # Connection authenticates as the App's own service principal (OAuth M2M).
        assert ws.posts()[0]["options"]["client_id"] == "sp-client-1"
        assert ws.service_principal_secrets_proxy.created == ["111222333"]

    def test_dry_run_creates_nothing(self, tmp_path: Path, fake_ws: Any) -> None:
        inst = _write_instance(tmp_path, _spec())
        ws = fake_ws()
        RestMcpPayloadAdapter().after_reconcile(ws, "my_proxy", self._stash(inst), dry_run=True)
        assert ws.api_client.calls == []

    def test_noop_without_stashed_context(self, fake_ws: Any) -> None:
        ws = fake_ws()
        RestMcpPayloadAdapter().after_reconcile(ws, "my_proxy", {}, dry_run=False)
        assert ws.api_client.calls == []

    def test_skips_when_app_url_unavailable(self, tmp_path: Path, fake_ws: Any) -> None:
        inst = _write_instance(tmp_path, _spec())
        ws = fake_ws(app_url="")
        RestMcpPayloadAdapter().after_reconcile(ws, "my_proxy", self._stash(inst), dry_run=False)
        assert ws.posts() == []


class TestDestroyAndState:
    def test_plan_destroy_lists_each_connection_destructive(self, tmp_path: Path) -> None:
        from agent_foundry.bricks import DestroyContext

        inst = _write_instance(tmp_path, _spec())
        ctx = DestroyContext(instance_dir=inst, target="dev", variables={}, dry_run=True, config=None, force=True)
        items = RestMcpPayloadAdapter().plan_destroy(ctx)
        assert len(items) == 1
        assert items[0].sub_instance == "orders"
        assert items[0].destructive is True
        assert "my_proxy_orders_mcp" in items[0].resource

    def test_destroy_deletes_connection(self, tmp_path: Path, fake_ws: Any) -> None:
        from agent_foundry.bricks import DeployContext, DestroyContext

        inst = _write_instance(tmp_path, _spec())
        ws = fake_ws()
        adapter = RestMcpPayloadAdapter()
        # Provision first so there is state + a live connection to remove.
        stash = {"rest_mcp__instance_dir": str(inst), "rest_mcp__target": "dev"}
        adapter.after_reconcile(ws, "my_proxy", stash, dry_run=False)

        deploy_ctx = DeployContext(instance_dir=inst, target="dev", variables={}, dry_run=False, config=None, ws=ws)
        assert adapter.load_state_outputs(deploy_ctx) == {"rest_mcp_connection__orders": "my_proxy_orders_mcp"}

        destroy_ctx = DestroyContext(
            instance_dir=inst, target="dev", variables={}, dry_run=False, config=None, ws=ws, force=True
        )
        results = adapter.destroy(destroy_ctx)
        assert [r.name for r in results] == ["rest_mcp:my_proxy_orders_mcp"]
        assert ws.deletes() == ["my_proxy_orders_mcp"]

    def test_load_state_outputs_empty_before_provision(self, tmp_path: Path, fake_ws: Any) -> None:
        from agent_foundry.bricks import DeployContext

        inst = _write_instance(tmp_path, _spec())
        ctx = DeployContext(instance_dir=inst, target="dev", variables={}, dry_run=False, config=None, ws=fake_ws())
        assert RestMcpPayloadAdapter().load_state_outputs(ctx) == {}


def _spec_with_service() -> dict[str, Any]:
    spec = _spec()
    spec["apis"][0]["mcp_service"] = {
        "enabled": True,
        "include_tool_selectors": ["get_*"],
        "grants": {"execute": ["group:analysts"]},
    }
    return spec


class TestMcpServiceOrchestration:
    """The optional path-C layer is orchestrated after connections and torn
    down before them (reverse of create)."""

    def _stash(self, inst: Path) -> dict[str, str]:
        return {
            "rest_mcp__instance_dir": str(inst),
            "rest_mcp__target": "dev",
            "catalog": "cat",
            "schema_prefix": "pfx",
        }

    def test_after_reconcile_provisions_connection_then_service(self, tmp_path: Path, fake_ws: Any) -> None:
        inst = _write_instance(tmp_path, _spec_with_service())
        ws = fake_ws()
        RestMcpPayloadAdapter().after_reconcile(ws, "my_proxy", self._stash(inst), dry_run=False)
        # Connection exists and the service was built from it.
        assert "my_proxy_orders_mcp" in ws.api_client.existing
        svc = ws.api_client.services["cat.pfx_rest_mcp.orders"]
        assert svc["config"]["connection"]["name"] == "connections/my_proxy_orders_mcp"

    def test_load_state_outputs_publishes_connection_and_service(self, tmp_path: Path, fake_ws: Any) -> None:
        from agent_foundry.bricks import DeployContext

        inst = _write_instance(tmp_path, _spec_with_service())
        ws = fake_ws()
        adapter = RestMcpPayloadAdapter()
        adapter.after_reconcile(ws, "my_proxy", self._stash(inst), dry_run=False)
        ctx = DeployContext(instance_dir=inst, target="dev", variables={}, dry_run=False, config=None, ws=ws)
        assert adapter.load_state_outputs(ctx) == {
            "rest_mcp_connection__orders": "my_proxy_orders_mcp",
            "rest_mcp_service__orders": "cat.pfx_rest_mcp.orders",
        }

    def test_plan_destroy_lists_service_before_connection(self, tmp_path: Path, fake_ws: Any) -> None:
        from agent_foundry.bricks import DestroyContext

        inst = _write_instance(tmp_path, _spec_with_service())
        ws = fake_ws()
        adapter = RestMcpPayloadAdapter()
        adapter.after_reconcile(ws, "my_proxy", self._stash(inst), dry_run=False)
        ctx = DestroyContext(
            instance_dir=inst, target="dev", variables={}, dry_run=True, config=None, ws=ws, force=True
        )
        items = adapter.plan_destroy(ctx)
        assert [it.resource for it in items] == [
            "UC MCP Service cat.pfx_rest_mcp.orders",
            "UC MCP connection my_proxy_orders_mcp",
        ]

    def test_destroy_tears_down_service_before_connection(self, tmp_path: Path, fake_ws: Any) -> None:
        from agent_foundry.bricks import DestroyContext

        inst = _write_instance(tmp_path, _spec_with_service())
        ws = fake_ws()
        adapter = RestMcpPayloadAdapter()
        adapter.after_reconcile(ws, "my_proxy", self._stash(inst), dry_run=False)
        ctx = DestroyContext(
            instance_dir=inst, target="dev", variables={}, dry_run=False, config=None, ws=ws, force=True
        )
        results = adapter.destroy(ctx)
        assert [r.name for r in results] == [
            "rest_mcp:service:cat.pfx_rest_mcp.orders",
            "rest_mcp:my_proxy_orders_mcp",
        ]
        # Service DELETE precedes connection DELETE (reverse of create order).
        assert ws.deletes() == ["cat.pfx_rest_mcp.orders", "my_proxy_orders_mcp"]
        assert [(ref.kind, ref.resource_id) for r in results for ref in r.deleted] == [
            ("mcp_service", "cat.pfx_rest_mcp.orders"),
            ("uc_connection", "my_proxy_orders_mcp"),
        ]
