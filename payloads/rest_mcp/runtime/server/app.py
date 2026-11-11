"""Assemble the multi-API MCP host as one Starlette ASGI app.

One hosting App fronts N REST APIs; each becomes its own Streamable-HTTP MCP
surface mounted at ``/mcp/<api>`` (matching the UC connection / MCP-Service
target the deploy adapter registers).  Every mount is a low-level MCP server
(:mod:`payloads.rest_mcp.runtime.server.registry`) wrapped in a
``StreamableHTTPSessionManager`` running **stateless** -- horizontally
scalable and Databricks-Assistant compatible.

Seams keep this testable without a network or the Databricks SDK:

* ``client_factory`` -- builds the per-API ``httpx.AsyncClient`` (default honours
  the API's ``timeout_s``); tests inject a ``MockTransport`` client.
* ``secret_resolver`` -- resolves ``static`` bearer secrets; tests inject a fake.

The shared ``httpx`` clients and the session managers' task groups are owned by
the app lifespan, so they start and drain with the process.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from payloads.rest_mcp.contracts import ApiSpec, RestMcpPayloadSpec
from payloads.rest_mcp.runtime.server.auth import (
    ForwardedIdentityMiddleware,
    SecretResolver,
    default_secret_resolver,
    make_token_provider,
)
from payloads.rest_mcp.runtime.server.registry import build_api_server

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

ClientFactory = Callable[[ApiSpec], httpx.AsyncClient]


def default_client_factory(api: ApiSpec) -> httpx.AsyncClient:
    """One client per API, defaulting the transport timeout to ``timeout_s``."""
    return httpx.AsyncClient(timeout=api.rest.defaults.timeout_s)


@dataclass(frozen=True)
class _Mounted:
    manager: StreamableHTTPSessionManager
    client: httpx.AsyncClient


def build_app(
    spec: RestMcpPayloadSpec,
    instance_root: Path,
    *,
    secret_resolver: SecretResolver = default_secret_resolver,
    client_factory: ClientFactory = default_client_factory,
) -> Starlette:
    """Build the Starlette app fronting every API in ``spec``.

    Mounts ``/mcp/<api>`` per API plus a ``/health`` probe; the OBO
    ``x-forwarded-access-token`` is captured per request by
    :class:`ForwardedIdentityMiddleware`.
    """
    mounted: dict[str, _Mounted] = {}
    for api in spec.apis:
        client = client_factory(api)
        server = build_api_server(
            api,
            instance_root,
            client=client,
            token_provider=make_token_provider(api.rest.auth, secret_resolver),
        )
        manager = StreamableHTTPSessionManager(app=server, stateless=True)
        mounted[api.name] = _Mounted(manager=manager, client=client)

    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            for entry in mounted.values():
                await stack.enter_async_context(entry.manager.run())
                stack.push_async_callback(entry.client.aclose)
            yield

    async def health(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "apis": sorted(mounted)})

    routes: list[Route | Mount] = [Route("/health", health, methods=["GET"])]
    routes += [Mount(f"/mcp/{name}", app=entry.manager.handle_request) for name, entry in mounted.items()]

    return Starlette(
        routes=routes,
        lifespan=lifespan,
        middleware=[Middleware(ForwardedIdentityMiddleware)],
    )


__all__ = ["ClientFactory", "build_app", "default_client_factory"]
