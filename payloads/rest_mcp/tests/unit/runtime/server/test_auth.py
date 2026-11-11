"""Tests for the host auth wiring: token providers + OBO capture."""

from __future__ import annotations

import base64
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from payloads.rest_mcp.contracts import NoAuth, OboAuth, StaticAuth
from payloads.rest_mcp.runtime.server.auth import (
    ForwardedIdentityMiddleware,
    current_obo_token,
    default_secret_resolver,
    make_token_provider,
)


def _ws_returning(value: str | None) -> MagicMock:
    ws = MagicMock()
    ws.secrets.get_secret.return_value.value = value
    return ws


class TestDefaultSecretResolver:
    def test_resolves_through_shared_factory(self) -> None:
        ws = _ws_returning(base64.b64encode(b"s3cret").decode())
        with patch("agent_foundry.workspace_client.get_workspace_client", return_value=ws) as factory:
            assert default_secret_resolver("sc", "ky") == "s3cret"
        factory.assert_called_once_with()
        ws.secrets.get_secret.assert_called_once_with(scope="sc", key="ky")

    def test_missing_value_raises(self) -> None:
        with (
            patch("agent_foundry.workspace_client.get_workspace_client", return_value=_ws_returning(None)),
            pytest.raises(RuntimeError, match="sc/ky has no value"),
        ):
            default_secret_resolver("sc", "ky")


class TestTokenProvider:
    def test_none_yields_no_token(self) -> None:
        provider = make_token_provider(NoAuth(), lambda _s, _k: "unused")
        assert provider() is None

    def test_static_resolves_secret_once(self) -> None:
        calls: list[tuple[str, str]] = []

        def resolver(scope: str, key: str) -> str:
            calls.append((scope, key))
            return "s3cret"

        provider = make_token_provider(StaticAuth(mode="static", secret_scope="sc", secret_key="ky"), resolver)
        assert provider() == "s3cret"
        assert provider() == "s3cret"
        assert calls == [("sc", "ky")]  # resolved eagerly, once

    def test_obo_reads_forwarded_token(self) -> None:
        provider = make_token_provider(OboAuth(mode="obo"), lambda _s, _k: "unused")
        seen: dict[str, Any] = {}

        async def endpoint(_: Request) -> JSONResponse:
            seen["token"] = provider()
            return JSONResponse({})

        app = Starlette(
            routes=[Route("/x", endpoint)],
            middleware=[Middleware(ForwardedIdentityMiddleware)],
        )
        with TestClient(app) as client:
            client.get("/x", headers={"x-forwarded-access-token": "obo-xyz"})
        assert seen["token"] == "obo-xyz"


def test_middleware_scopes_token_to_request() -> None:
    seen: dict[str, Any] = {}

    async def endpoint(_: Request) -> JSONResponse:
        seen["during"] = current_obo_token()
        return JSONResponse({})

    app = Starlette(
        routes=[Route("/x", endpoint)],
        middleware=[Middleware(ForwardedIdentityMiddleware)],
    )
    with TestClient(app) as client:
        client.get("/x", headers={"x-forwarded-access-token": "tok-123"})

    assert seen["during"] == "tok-123"
    assert current_obo_token() is None  # cleared after the request
