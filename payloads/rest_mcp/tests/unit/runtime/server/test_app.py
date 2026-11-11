"""Tests for the multi-API ASGI host.

Driven with a Starlette ``TestClient`` over ``MockTransport`` clients: the
health probe lists mounts, unknown mounts 404, and a real MCP ``initialize``
handshake against a mount proves the Streamable-HTTP wiring.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
from starlette.testclient import TestClient

from payloads.rest_mcp.contracts import RestMcpPayloadSpec
from payloads.rest_mcp.runtime.server.app import ClientFactory, build_app

_INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}


def _spec(*names: str) -> RestMcpPayloadSpec:
    return RestMcpPayloadSpec.model_validate(
        {
            "apis": [
                {
                    "name": name,
                    "rest": {
                        "base_url": f"https://{name}.example.com",
                        "allowed_hosts": [f"{name}.example.com"],
                        "auth": {"mode": "none"},
                    },
                    "tools": [
                        {
                            "name": "get_status",
                            "description": "Get status.",
                            "request": {"method": "GET", "path": "/status"},
                        }
                    ],
                }
                for name in names
            ]
        }
    )


def _factory() -> ClientFactory:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok"})

    return lambda _api: httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _build(names: tuple[str, ...], tmp_path: Path) -> TestClient:
    app = build_app(
        _spec(*names),
        tmp_path,
        secret_resolver=lambda _s, _k: "unused",
        client_factory=_factory(),
    )
    return TestClient(app)


def test_health_lists_mounted_apis(tmp_path: Path) -> None:
    with _build(("orders", "inventory"), tmp_path) as client:
        response = client.get("/health")
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    assert body["status"] == "ok"
    assert body["apis"] == ["inventory", "orders"]


def test_unknown_mount_returns_404(tmp_path: Path) -> None:
    with _build(("orders",), tmp_path) as client:
        assert client.get("/mcp/nope").status_code == 404


def test_initialize_handshake(tmp_path: Path) -> None:
    with _build(("orders",), tmp_path) as client:
        response = client.post(
            "/mcp/orders",
            json=_INITIALIZE,
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
        )
    assert response.status_code == 200
    assert "text/event-stream" in response.headers["content-type"]
    assert '"serverInfo"' in response.text
    assert "orders" in response.text
