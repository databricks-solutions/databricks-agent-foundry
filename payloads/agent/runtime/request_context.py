"""Per-request state, forwarded identity headers and workspace clients."""

from __future__ import annotations

import dataclasses
import functools
import logging
import os
from typing import Any

from databricks.sdk import WorkspaceClient

from agent_foundry import env
from agent_foundry.telemetry import product_kwargs
from payloads.agent.runtime.gateway_resources import UserTokenRequiredError

logger = logging.getLogger(__name__)


@dataclasses.dataclass
class RequestState:
    """Request-scoped state threaded through ``Runner.run(context=...)``.

    The framework constructs one instance per request, passes it as the
    OpenAI-Agents run ``context`` (so every ``type: python`` tool call in
    the run shares it), and -- on the ``@invoke`` / ``@stream`` path -- hands
    the same instance to the ``reset`` and ``finalize`` hooks.

    ``data`` is an opaque bag the framework never inspects: instances stash
    their own per-request objects there (e.g. ``state.data['spec']``) and
    read them back in ``finalize``.  ``request`` is the
    ``ResponsesAgentRequest`` on the ``@invoke`` / ``@stream`` path and
    ``None`` on the ``run_agent`` / REPL paths (where no request object and
    no reset/finalize hooks exist), so a tool reading it must tolerate
    ``None`` and use ``data.setdefault(...)`` rather than assume ``reset``
    seeded anything.
    """

    request: Any = None
    data: dict[str, Any] = dataclasses.field(default_factory=dict)


def new_request_state(request: Any = None) -> "RequestState":
    """Construct a fresh :class:`RequestState` for one request."""
    return RequestState(request=request)


# ---------------------------------------------------------------------------
# Workspace clients
# ---------------------------------------------------------------------------


def get_workspace_client() -> WorkspaceClient:
    """Build a WorkspaceClient from the environment (service principal).

    When using a CLI profile, resolve a bearer token and export it as
    ``DATABRICKS_HOST`` / ``DATABRICKS_TOKEN`` env vars.  This ensures
    that libraries which create their own SDK clients internally
    (e.g. ``VectorSearchRetrieverTool`` from ``databricks_openai``)
    pick up valid credentials in local-dev and CLI evaluation contexts.
    """
    extra: Any = product_kwargs()
    profile = os.environ.get("DATABRICKS_CONFIG_PROFILE")
    if profile:
        ws = WorkspaceClient(profile=profile, **extra)
        try:
            headers = ws.config.authenticate()
            token = headers.get("Authorization", "").removeprefix("Bearer ").strip()
            if token:
                os.environ["DATABRICKS_TOKEN"] = token
                os.environ.setdefault("DATABRICKS_HOST", ws.config.host)
                return WorkspaceClient(
                    host=ws.config.host,
                    token=token,
                    auth_type="pat",
                    **extra,
                )
        except Exception:
            pass
        return ws
    return _environment_workspace_client()


@functools.cache
def _environment_workspace_client() -> WorkspaceClient:
    """Service-principal client from the App environment, built once per process.

    Construction resolves auth over HTTP on every call; the SDK refreshes
    its OAuth token itself, so one shared client is safe.  The profile
    branch above is not cached: it pins a bearer token resolved once.
    """
    extra: Any = product_kwargs()
    return WorkspaceClient(**extra)


def _get_all_request_headers() -> dict[str, str]:
    """Merge MLflow agent-server headers with the agent_runner fallback.

    The ``@invoke()``/``@stream()`` path populates the MLflow context
    variable; MCP/A2A paths populate the ``agent_runner`` context var
    via :func:`payloads.agent.runtime.agent_runner.set_request_headers`.
    """
    headers: dict[str, str] = {}
    try:
        from payloads.agent.runtime.agent_runner import get_fallback_headers

        headers.update(get_fallback_headers())
    except (ImportError, Exception):
        pass
    try:
        from mlflow.genai.agent_server import get_request_headers

        headers.update(get_request_headers())
    except (ImportError, Exception):
        pass
    return headers


def get_forwarded_headers() -> dict[str, str]:
    """The current request's proxy headers, for callers that need several of them."""
    return _get_all_request_headers()


def get_obo_token() -> str | None:
    """Extract the OBO user token from the ``x-forwarded-access-token`` header.

    Returns ``None`` outside a Databricks App (local dev, tests).
    """
    return _get_all_request_headers().get("x-forwarded-access-token") or None


def get_request_username() -> str | None:
    """Extract the display name of the authenticated user from OBO headers.

    Returns ``None`` outside a Databricks App (local dev, tests).
    """
    raw = _get_all_request_headers().get("x-forwarded-preferred-username")
    return _decode_header_value(raw) if raw else None


def _decode_header_value(value: str) -> str:
    """Recover UTF-8 text from a header the server decoded as latin-1.

    HTTP header values are latin-1 by specification, so a name carrying
    anything outside ASCII -- "Café" -- arrives mangled as "CafÃ©" and goes
    straight into the prompt that way.  Round-tripping through latin-1
    undoes it.  A value that is genuinely latin-1, or already correct, does
    not survive the round trip and is returned untouched.
    """
    try:
        return value.encode("latin-1").decode("utf-8")
    except (UnicodeDecodeError, UnicodeEncodeError):
        return value


def get_user_workspace_client() -> WorkspaceClient | None:
    """Build a user-scoped WorkspaceClient from the OBO token.

    Returns ``None`` when no OBO token is available (local dev).
    """
    token = get_obo_token()
    if not token:
        return None
    logger.info("[auth] using OBO token for tool calls")
    return user_workspace_client(token)


def user_workspace_client(token: str) -> WorkspaceClient:
    """A WorkspaceClient acting as the user whose OBO *token* this is."""
    extra: Any = product_kwargs()
    return WorkspaceClient(token=token, auth_type="pat", **extra)


_USER_TOKEN_REQUIRED = (
    "This agent's tools run as the signed-in user, but the request carried no user token. "
    "Add the required user_api_scopes to the Databricks App so the user's token is forwarded."
)


def resolve_tools_client(sp_ws: WorkspaceClient) -> WorkspaceClient:
    """Return the WorkspaceClient that tool calls and workspace MCP servers run as.

    The user's OBO client when a token is present.  Without one, local
    development uses *sp_ws* -- the developer's own CLI profile -- while a
    Databricks App raises :class:`UserTokenRequiredError`: running as the
    app's service principal would hand the user its permissions.
    """
    user_ws = get_user_workspace_client()
    if user_ws:
        return user_ws
    if env.on_databricks_apps():
        raise UserTokenRequiredError(_USER_TOKEN_REQUIRED)
    return sp_ws
