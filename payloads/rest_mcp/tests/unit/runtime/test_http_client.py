"""Tests for the code-tool ``HttpClient`` seam.

Exercised with an ``httpx.MockTransport`` (no network).  The key guarantees:
authed API calls carry the hop-2 header, the un-authed ``fetch_bytes`` does
*not* leak the token to an allow-listed asset host, and every path shares the
engine's egress + byte-cap rails.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from payloads.rest_mcp.contracts import RestConfig
from payloads.rest_mcp.runtime.engine import EgressError, UpstreamError
from payloads.rest_mcp.runtime.http_client import HttpClient


def _rest(**overrides: Any) -> RestConfig:
    base: dict[str, Any] = {
        "base_url": "https://api.example.com",
        "allowed_hosts": ["api.example.com"],
        "auth": {"mode": "static", "secret_scope": "s", "secret_key": "k"},
    }
    base.update(overrides)
    return RestConfig.model_validate(base)


def _client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class TestAuthedCalls:
    @pytest.mark.asyncio
    async def test_get_json_is_authed(self) -> None:
        seen: dict[str, httpx.Request] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["req"] = request
            return httpx.Response(200, json={"ok": True})

        async with _client(handler) as client:
            http = HttpClient(_rest(), token="TKN", client=client)
            body = await http.get_json("/status", params={"verbose": True})

        assert body == {"ok": True}
        assert str(seen["req"].url) == "https://api.example.com/status?verbose=true"
        assert seen["req"].headers["Authorization"] == "Bearer TKN"

    @pytest.mark.asyncio
    async def test_post_json_sends_body_and_auth(self) -> None:
        seen: dict[str, httpx.Request] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["req"] = request
            return httpx.Response(200, json={"echo": True})

        async with _client(handler) as client:
            http = HttpClient(_rest(), token="TKN", client=client)
            out = await http.post_json("/invocations", {"rows": [1, 2]})

        assert out == {"echo": True}
        assert seen["req"].headers["Authorization"] == "Bearer TKN"
        import json

        assert json.loads(seen["req"].content) == {"rows": [1, 2]}

    @pytest.mark.asyncio
    async def test_4xx_raises_upstream_error(self) -> None:
        async with _client(lambda r: httpx.Response(404, text="nope")) as client:
            http = HttpClient(_rest(), token="TKN", client=client)
            with pytest.raises(UpstreamError) as exc_info:
                await http.get_json("/missing")
        assert exc_info.value.status_code == 404


class TestFetchBytes:
    @pytest.mark.asyncio
    async def test_fetch_bytes_does_not_leak_token(self) -> None:
        seen: dict[str, httpx.Request] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["req"] = request
            return httpx.Response(200, content=b"\x89PNG...")

        async with _client(handler) as client:
            http = HttpClient(_rest(), token="SECRET", client=client)
            data = await http.fetch_bytes("/asset/1.png")

        assert data == b"\x89PNG..."
        assert "authorization" not in {k.lower() for k in seen["req"].headers}

    @pytest.mark.asyncio
    async def test_fetch_bytes_over_cap_rejected(self) -> None:
        rest = _rest(defaults={"max_response_bytes": 8})
        async with _client(lambda r: httpx.Response(200, content=b"x" * 100)) as client:
            http = HttpClient(rest, token="TKN", client=client)
            with pytest.raises(UpstreamError, match="exceeds max_response_bytes"):
                await http.fetch_bytes("/big")

    @pytest.mark.asyncio
    async def test_fetch_bytes_egress_guard(self) -> None:
        rest = _rest()
        async with _client(lambda r: httpx.Response(200, content=b"x")) as client:
            http = HttpClient(rest, token="TKN", client=client)
            with pytest.raises(EgressError, match="allowed_hosts"):
                await http.fetch_bytes("https://cdn.evil.example.com/x.png")


class TestUrlResolution:
    def test_relative_and_absolute(self) -> None:
        http = HttpClient(_rest(), token=None, client=httpx.AsyncClient())
        assert http._resolve_url("/x") == "https://api.example.com/x"
        assert http._resolve_url("x") == "https://api.example.com/x"
        assert http._resolve_url("https://api.example.com/y") == "https://api.example.com/y"
