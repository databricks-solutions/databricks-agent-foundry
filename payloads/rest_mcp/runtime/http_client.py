"""The ``HttpClient`` seam injected into Tier-3 code tools.

A code tool never builds its own transport: it receives an ``HttpClient``
pre-bound to one API's ``RestConfig`` (auth + ``allowed_hosts`` + byte cap) and
the shared ``httpx`` client.  Every call routes through the **same engine
rails** a declarative tool uses (:func:`~payloads.rest_mcp.runtime.engine.guard_egress`,
:func:`~payloads.rest_mcp.runtime.engine.build_auth_header`,
:func:`~payloads.rest_mcp.runtime.engine.cap_text`), so a bespoke tool cannot
get weaker enforcement than a declared one.

Two families of helpers make the auth boundary explicit:

* **Authed API calls** (``get_json`` / ``post_json`` / :meth:`request`) target
  the API and carry the hop-2 auth header.
* **Un-authed asset fetch** (:meth:`fetch_bytes`) pulls a resource from an
  allow-listed host **without** the API token -- so a tool that needs to read,
  e.g., an image by URL never leaks the API credential to a third-party host.
  The target host must still be in ``allowed_hosts`` (https-only).
"""

from __future__ import annotations

from typing import Any

import httpx

from payloads.rest_mcp.contracts import RestConfig
from payloads.rest_mcp.runtime.engine import (
    DEFAULT_USER_AGENT,
    UpstreamError,
    build_auth_header,
    cap_text,
    guard_egress,
)

_ABSOLUTE_URL_PREFIXES = ("http://", "https://")


class HttpClient:
    """Authenticated, egress-guarded HTTP facade bound to one API's config."""

    def __init__(self, rest: RestConfig, *, token: str | None, client: httpx.AsyncClient) -> None:
        self._rest = rest
        self._token = token
        self._client = client

    def _resolve_url(self, path_or_url: str) -> str:
        if path_or_url.startswith(_ABSOLUTE_URL_PREFIXES):
            return path_or_url
        path = path_or_url if path_or_url.startswith("/") else f"/{path_or_url}"
        return f"{self._rest.base_url.rstrip('/')}{path}"

    def _raise_for_status(self, response: httpx.Response) -> None:
        if response.status_code >= 400:
            raise UpstreamError(
                response.status_code,
                cap_text(response.text, self._rest.defaults.max_response_bytes),
            )

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
    ) -> httpx.Response:
        """Make an authed request to the API, egress-guarded, raising on 4xx/5xx."""
        url = self._resolve_url(path)
        guard_egress(self._rest, url)
        headers = build_auth_header(self._rest, self._token)
        headers.setdefault("User-Agent", DEFAULT_USER_AGENT)
        request = httpx.Request(
            method,
            url,
            params=params,
            json=json,
            headers=headers,
        )
        response = await self._client.send(request)
        self._raise_for_status(response)
        return response

    async def get_json(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        """GET the API and return the parsed JSON body."""
        response = await self.request("GET", path, params=params)
        return response.json()

    async def post_json(self, path: str, json_body: Any) -> Any:
        """POST a JSON body to the API and return the parsed JSON response."""
        response = await self.request("POST", path, json=json_body)
        return response.json()

    async def fetch_bytes(self, url: str) -> bytes:
        """Fetch raw bytes from an allow-listed host **without** the API token.

        For pulling assets (images, files) a tool needs as *input*.  Egress is
        guarded (https + ``allowed_hosts``) and the payload is bounded by
        ``max_response_bytes`` -- an over-cap asset raises rather than being
        silently truncated (which would corrupt binary content).
        """
        target = self._resolve_url(url)
        guard_egress(self._rest, target)
        response = await self._client.send(httpx.Request("GET", target))
        self._raise_for_status(response)
        cap = self._rest.defaults.max_response_bytes
        content = response.content
        if len(content) > cap:
            raise UpstreamError(413, f"asset exceeds max_response_bytes ({len(content)} > {cap})")
        return content


__all__ = ["HttpClient"]
