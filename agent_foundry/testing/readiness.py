"""Poll Databricks Apps until the deployed payload is ready."""

from __future__ import annotations

import json
import time
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from agent_foundry.testing.workspace import WorkspaceBinding, assert_host_matches_sdk

_GATEWAY_NOT_READY = frozenset({502, 503, 504})


class ReadinessError(TimeoutError):
    """App did not reach a usable state within the timeout."""


def wait_for_app(
    ws: Any,
    app_name: str,
    *,
    binding: WorkspaceBinding | None = None,
    timeout_seconds: float = 900,
    poll_seconds: float = 10,
) -> str:
    """Return the App URL once ``app_status.state`` is RUNNING and the App answers HTTP.

    The Apps API reports RUNNING (and the deployment SUCCEEDED) while the
    App's start script is still installing and booting its server; until
    a process listens, the Apps proxy answers 502.

    *binding* guards env-configured callers against a client that resolved
    a different workspace; callers whose client comes from the target
    config pass ``None``.
    """
    if binding is not None:
        assert_host_matches_sdk(ws, binding)
    deadline = time.monotonic() + timeout_seconds
    last = "not-queried"
    while time.monotonic() < deadline:
        app = ws.apps.get(app_name)
        last = _app_state(app)
        url = getattr(app, "url", None) or ""
        if _is_ready(last) and url:
            if _answers_http(ws, url):
                return str(url)
            last = f"{last} (proxy not reaching the App yet)"
        time.sleep(poll_seconds)
    raise ReadinessError(f"app {app_name!r} not ready after {timeout_seconds:.0f}s (last state={last!r})")


def _answers_http(ws: Any, url: str) -> bool:
    """True once the App's ``/health`` answers with anything but a gateway error.

    ``/health`` is served by the payload's own server (both bundled
    payloads expose it), not the chat UI behind it.
    """
    try:
        authenticated_get(ws, f"{app_origin(url)}/health", timeout_seconds=30)
    except HTTPError as exc:
        return exc.code not in _GATEWAY_NOT_READY
    except OSError:
        return False
    return True


def authenticated_get(ws: Any, url: str, *, timeout_seconds: float = 60) -> bytes:
    """GET *url* with the workspace client's auth headers; return response body."""
    headers = dict(ws.config.authenticate())
    request = Request(url, headers=headers, method="GET")
    with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
        return bytes(response.read())


def authenticated_post_json(
    ws: Any,
    url: str,
    body: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
    timeout_seconds: float = 120,
) -> dict[str, Any]:
    """POST *body* as JSON with the workspace client's auth headers; return the JSON reply."""
    raw = _send_json(ws, url, body, method="POST", headers=headers, timeout_seconds=timeout_seconds)
    reply: dict[str, Any] = json.loads(raw.decode())
    return reply


def authenticated_put_json(ws: Any, url: str, body: dict[str, Any], *, timeout_seconds: float = 60) -> bytes:
    """PUT *body* as JSON with the workspace client's auth headers; return the raw reply (often empty)."""
    return _send_json(ws, url, body, method="PUT", headers=None, timeout_seconds=timeout_seconds)


def authenticated_post_bytes(
    ws: Any,
    url: str,
    data: bytes,
    *,
    content_type: str,
    headers: dict[str, str] | None = None,
    timeout_seconds: float = 120,
) -> bytes:
    """POST raw *data* (e.g. an audio clip) with the workspace client's auth headers; return the raw reply."""
    request_headers = {"Content-Type": content_type, **(headers or {})}
    return _send(ws, url, data, method="POST", headers=request_headers, timeout_seconds=timeout_seconds)


def _send_json(
    ws: Any,
    url: str,
    body: dict[str, Any],
    *,
    method: str,
    headers: dict[str, str] | None,
    timeout_seconds: float,
) -> bytes:
    request_headers = {"Content-Type": "application/json", **(headers or {})}
    data = json.dumps(body).encode()
    return _send(ws, url, data, method=method, headers=request_headers, timeout_seconds=timeout_seconds)


def _send(
    ws: Any,
    url: str,
    data: bytes,
    *,
    method: str,
    headers: dict[str, str],
    timeout_seconds: float,
) -> bytes:
    request = Request(url, data=data, headers={**dict(ws.config.authenticate()), **headers}, method=method)
    with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
        return bytes(response.read())


def app_origin(url: str) -> str:
    """Normalize an App URL to ``scheme://netloc``."""
    parsed = urlparse(url)
    if not parsed.netloc:
        raise ValueError(f"could not parse app URL {url!r}")
    scheme = parsed.scheme or "https"
    return f"{scheme}://{parsed.netloc}"


def _app_state(app: Any) -> str:
    status = getattr(app, "app_status", None) or getattr(app, "status", None)
    state = getattr(status, "state", None) if status is not None else None
    if state is None:
        return "unknown"
    return str(getattr(state, "value", state)).upper()


def _is_ready(state: str) -> bool:
    return state in {"RUNNING", "READY", "ACTIVE", "APP_STATE_RUNNING"}


__all__ = [
    "ReadinessError",
    "app_origin",
    "authenticated_get",
    "authenticated_post_bytes",
    "authenticated_post_json",
    "wait_for_app",
]
