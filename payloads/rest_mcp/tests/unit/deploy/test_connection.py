"""Tests for the governed ``is_mcp`` UC connection provisioning (issue #326).

Exercises the SDK-thin logic against the :class:`FakeWorkspaceClient` fixture:
the exact ``options`` shape proven during the workspace spike, idempotent
create-if-absent, workspace-state round-trip, and teardown.
"""

from __future__ import annotations

from typing import Any

from payloads.rest_mcp.contracts import RestMcpPayloadSpec
from payloads.rest_mcp.deploy.connection import (
    build_connection_options,
    connection_name,
    destroy_connections,
    loaded_connections,
    provision_connections,
)


def _api(name: str) -> dict[str, Any]:
    return {
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
                "response": {"project": ["status"]},
            }
        ],
    }


def _spec(*names: str) -> RestMcpPayloadSpec:
    return RestMcpPayloadSpec.model_validate({"apis": [_api(n) for n in (names or ("orders",))]})


class TestPureHelpers:
    def test_connection_name_sanitizes_instance(self) -> None:
        assert connection_name("my_proxy", "orders") == "my_proxy_orders_mcp"
        assert connection_name("My.Proxy", "orders") == "my_proxy_orders_mcp"
        assert connection_name("keep-hyphen", "inventory") == "keep-hyphen_inventory_mcp"

    def test_build_options_targets_per_api_mcp_mount(self) -> None:
        opts = build_connection_options(
            app_url="https://app.example.com/",
            api_name="orders",
            app_sp_client_id="sp-client-abc",
            client_secret="sp-secret-xyz",
            workspace_host="https://ws.example.com/",
        )
        assert opts == {
            "host": "https://app.example.com",
            "base_path": "/mcp/orders/",
            "is_mcp_connection": "true",
            "client_id": "sp-client-abc",
            "client_secret": "sp-secret-xyz",
            "token_endpoint": "https://ws.example.com/oidc/v1/token",
            "oauth_scope": "all-apis",
        }


class TestProvision:
    def test_creates_one_connection_per_api(self, fake_ws: Any) -> None:
        ws = fake_ws()
        names = provision_connections(
            ws,
            _spec("orders", "inventory"),
            instance_name="my_proxy",
            target="dev",
            app_url="https://app.example.com",
            app_sp_client_id="sp-client-abc",
            app_sp_id="111222333",
            workspace_host="https://ws.example.com",
        )
        assert names == ["my_proxy_orders_mcp", "my_proxy_inventory_mcp"]
        posts = ws.posts()
        assert [p["name"] for p in posts] == names
        assert posts[0]["connection_type"] == "HTTP"
        assert posts[0]["options"]["base_path"] == "/mcp/orders/"
        assert posts[0]["options"]["is_mcp_connection"] == "true"
        assert posts[0]["options"]["client_id"] == "sp-client-abc"
        # One M2M secret minted per created connection; the secret flows into options.
        assert ws.service_principal_secrets_proxy.created == ["111222333", "111222333"]
        assert posts[0]["options"]["client_secret"] == "sp-secret-1"

    def test_skips_create_when_connection_exists(self, fake_ws: Any) -> None:
        ws = fake_ws(
            existing={
                "my_proxy_orders_mcp": {
                    "name": "my_proxy_orders_mcp",
                    "options": {
                        "host": "https://app.example.com",
                        "base_path": "/mcp/orders/",
                        "client_id": "sp-client-abc",
                    },
                }
            }
        )
        provision_connections(
            ws,
            _spec("orders"),
            instance_name="my_proxy",
            target="dev",
            app_url="https://app.example.com",
            app_sp_client_id="sp-client-abc",
            app_sp_id="111222333",
            workspace_host="https://ws.example.com",
        )
        assert ws.posts() == []
        assert ws.connection_patches() == []
        # No secret minted when the connection already exists.
        assert ws.service_principal_secrets_proxy.created == []

    def test_refreshes_when_app_sp_rotated(self, fake_ws: Any) -> None:
        ws = fake_ws(
            existing={
                "my_proxy_orders_mcp": {
                    "name": "my_proxy_orders_mcp",
                    "options": {
                        "host": "https://app.example.com",
                        "base_path": "/mcp/orders/",
                        "client_id": "old-sp-client",
                    },
                }
            }
        )
        provision_connections(
            ws,
            _spec("orders"),
            instance_name="my_proxy",
            target="dev",
            app_url="https://app.example.com",
            app_sp_client_id="sp-client-abc",
            app_sp_id="111222333",
            workspace_host="https://ws.example.com",
        )
        assert ws.posts() == []
        patches = ws.connection_patches()
        assert len(patches) == 1
        assert patches[0]["options"]["client_id"] == "sp-client-abc"
        assert patches[0]["options"]["client_secret"] == "sp-secret-1"
        assert ws.service_principal_secrets_proxy.created == ["111222333"]
        assert loaded_connections(ws, _spec("orders"), instance_name="my_proxy", target="dev") == {
            "orders": "my_proxy_orders_mcp"
        }

    def test_dry_run_writes_nothing(self, fake_ws: Any) -> None:
        ws = fake_ws()
        names = provision_connections(
            ws,
            _spec("orders"),
            instance_name="my_proxy",
            target="dev",
            app_url="https://app.example.com",
            app_sp_client_id="sp-client-abc",
            app_sp_id="111222333",
            workspace_host="https://ws.example.com",
            dry_run=True,
        )
        assert names == ["my_proxy_orders_mcp"]
        assert ws.api_client.calls == []
        assert ws.service_principal_secrets_proxy.created == []

    def test_offline_client_none_returns_names(self) -> None:
        names = provision_connections(
            None,
            _spec("orders"),
            instance_name="my_proxy",
            target="dev",
            app_url="",
            app_sp_client_id="",
            app_sp_id="",
            workspace_host="",
            dry_run=True,
        )
        assert names == ["my_proxy_orders_mcp"]


class TestStateAndDestroy:
    def test_provision_state_roundtrips_and_destroy_clears(self, fake_ws: Any) -> None:
        ws = fake_ws()
        spec = _spec("orders")
        provision_connections(
            ws,
            spec,
            instance_name="my_proxy",
            target="dev",
            app_url="https://app.example.com",
            app_sp_client_id="sp-client-abc",
            app_sp_id="111222333",
            workspace_host="https://ws.example.com",
        )
        assert loaded_connections(ws, spec, instance_name="my_proxy", target="dev") == {"orders": "my_proxy_orders_mcp"}

        removed = destroy_connections(ws, spec, instance_name="my_proxy", target="dev")
        assert removed == ["my_proxy_orders_mcp"]
        assert ws.deletes() == ["my_proxy_orders_mcp"]
        assert loaded_connections(ws, spec, instance_name="my_proxy", target="dev") == {}
        # The minted secret is revoked on teardown (1:1 connection<->secret lifecycle).
        assert ws.service_principal_secrets_proxy.deleted == [("111222333", "secret-id-1")]

    def test_destroy_dry_run_deletes_nothing(self, fake_ws: Any) -> None:
        ws = fake_ws()
        removed = destroy_connections(ws, _spec("orders"), instance_name="my_proxy", target="dev", dry_run=True)
        assert removed == ["my_proxy_orders_mcp"]
        assert ws.deletes() == []
