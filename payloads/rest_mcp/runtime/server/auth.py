"""Hop-2 auth wiring for the host: token providers + OBO identity capture.

Separates *how the token is obtained* (this module) from *how it is applied*
(the engine's ``build_auth_header``).  Three cases, one per ``auth.mode``:

* ``none``   -> always ``None``.
* ``static`` -> a Databricks-secret bearer, resolved **once** at build time
  through the injected :data:`SecretResolver` seam (never inlined, never logged).
* ``obo``    -> the caller's ``x-forwarded-access-token``, captured per request
  by :class:`ForwardedIdentityMiddleware` into a context var.

The ``SecretResolver`` seam keeps the Databricks SDK out of the import path
(and out of tests): the default resolver imports it lazily, tests inject a fake.
"""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar

from starlette.types import ASGIApp, Receive, Scope, Send

from payloads.rest_mcp.contracts import OboAuth, RestAuth, StaticAuth
from payloads.rest_mcp.runtime.server.registry import TokenProvider

# Resolves a Databricks secret (scope, key) -> plaintext value.
SecretResolver = Callable[[str, str], str]

# Per-request OBO token, populated by ForwardedIdentityMiddleware.
_obo_token: ContextVar[str | None] = ContextVar("rest_mcp_obo_token", default=None)

_FORWARDED_TOKEN_HEADER = b"x-forwarded-access-token"


def current_obo_token() -> str | None:
    """Return the OBO token forwarded for the current request, if any."""
    return _obo_token.get()


def default_secret_resolver(scope: str, key: str) -> str:
    """Resolve a Databricks secret via the SDK (lazy import; in-container only)."""
    import base64

    from agent_foundry.workspace_client import get_workspace_client

    response = get_workspace_client().secrets.get_secret(scope=scope, key=key)
    if response.value is None:
        raise RuntimeError(f"secret {scope}/{key} has no value")
    return base64.b64decode(response.value).decode("utf-8")


def make_token_provider(auth: RestAuth, secret_resolver: SecretResolver) -> TokenProvider:
    """Build the hop-2 token provider for one API's auth mode.

    ``static`` secrets are resolved eagerly (once) so a missing secret fails at
    startup, not on the first tool call.
    """
    if isinstance(auth, StaticAuth):
        secret = secret_resolver(auth.secret_scope, auth.secret_key)
        return lambda: secret
    if isinstance(auth, OboAuth):
        return current_obo_token
    # NoAuth (and exhaustive fallback): no token.
    return lambda: None


class ForwardedIdentityMiddleware:
    """Capture the Databricks Apps proxy ``x-forwarded-access-token`` per request.

    A pure ASGI middleware (not ``BaseHTTPMiddleware``) so it composes cleanly
    with the Streamable-HTTP streaming responses.  The token lives only for the
    request scope and is reset afterwards, so it never leaks across requests.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        raw = headers.get(_FORWARDED_TOKEN_HEADER)
        token = raw.decode("latin-1") if raw else None
        reset = _obo_token.set(token)
        try:
            await self._app(scope, receive, send)
        finally:
            _obo_token.reset(reset)


__all__ = [
    "ForwardedIdentityMiddleware",
    "SecretResolver",
    "current_obo_token",
    "default_secret_resolver",
    "make_token_provider",
]
