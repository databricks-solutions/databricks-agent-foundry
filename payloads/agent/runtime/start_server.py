"""
Agent Server with Chat UI Proxy
=================================
Starts the MLflow AgentServer with ``enable_chat_proxy=True`` so it
serves both the ``/invocations`` API and proxies the bundled
``e2e-chatbot-app-next`` frontend on the same port.

The agent module (``AGENT_MODULE`` env) is imported so its
``@invoke()`` / ``@stream()`` decorators register with the server.

This script is called by ``start_app.py`` (the process manager) and
should not normally be invoked directly.

Usage::

    python bricks/app/scripts/start_server.py
    python bricks/app/scripts/start_server.py --port 8001 --reload
"""

from __future__ import annotations

import argparse
import importlib
import os
from typing import TYPE_CHECKING, Any

import httpx

from agent_foundry import env
from payloads.agent.runtime.chat_socket_proxy import register_chat_socket_route
from payloads.agent.runtime.extensions import load_extensions, merged_proxy_paths
from payloads.agent.runtime.health import register_health_route

if TYPE_CHECKING:
    from fastapi import FastAPI

# Read by MLflow's AgentServer while it builds the chat proxy, which
# otherwise 404s every path not on its allowlist -- including client-side
# routes an extension's UI overlay adds.
CHAT_PROXY_EXACT_PATHS_ENV = "CHAT_PROXY_ALLOWED_EXACT_PATHS"
CHAT_PROXY_PATH_PREFIXES_ENV = "CHAT_PROXY_ALLOWED_PATH_PREFIXES"


class Latin1HeaderClient(httpx.AsyncClient):
    """Chat-proxy client that forwards header values byte-for-byte.

    Starlette decodes header bytes as latin-1, so a display name such as
    "Café" in ``x-forwarded-preferred-username`` reaches the AgentServer chat
    proxy as "CafÃ©".  httpx encodes ``str`` header values as ASCII and raises
    on it, which the proxy turns into a 502 for every chat UI request.
    Re-encoding as latin-1 restores the original bytes.
    """

    def build_request(self, method: str, url: httpx.URL | str, **kwargs: Any) -> httpx.Request:
        headers = kwargs.get("headers")
        if isinstance(headers, dict):
            kwargs["headers"] = {
                key: value.encode("latin-1") if isinstance(value, str) else value for key, value in headers.items()
            }
        return super().build_request(method, url, **kwargs)


def _extend_allowlist(var: str, paths: tuple[str, ...]) -> None:
    """Append *paths* to the comma-separated allowlist in *var*, keeping existing entries."""
    allowed = [entry.strip() for entry in os.environ.get(var, "").split(",") if entry.strip()]
    allowed.extend(path for path in paths if path not in allowed)
    if allowed:
        os.environ[var] = ",".join(allowed)


def register_sidebar_routes(app: FastAPI) -> None:
    """The resources panel's routes; skill writes too, unless Lakebase user skills own them."""
    from payloads.agent.runtime.resources_routes import register_resources_routes
    from payloads.agent.runtime.skill_authoring_routes import register_skill_authoring_routes

    register_resources_routes(app)
    # Lakebase user skills are written through the chat UI server, which
    # the chat proxy reaches once this server leaves the path unclaimed.
    if not env.read_bool(env.ENABLE_USER_SKILLS):
        register_skill_authoring_routes(app)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Start AgentServer with chat proxy")
    parser.add_argument("--port", type=int, default=8000, help="Port (default: 8000)")
    parser.add_argument("--host", default="0.0.0.0", help="Host (default: 0.0.0.0)")
    parser.add_argument(
        "--agent-module",
        default=env.read(env.AGENT_MODULE),
        help="Python module with @invoke/@stream handlers",
    )
    parser.add_argument("--reload", action="store_true", help="Auto-reload on code changes")
    parser.add_argument("--workers", type=int, default=1, help="Number of uvicorn workers")
    args = parser.parse_args(argv)

    importlib.import_module(args.agent_module)

    from mlflow.genai.agent_server import AgentServer

    extensions = load_extensions()
    proxy_paths = merged_proxy_paths(extensions)
    _extend_allowlist(CHAT_PROXY_EXACT_PATHS_ENV, proxy_paths.exact)
    _extend_allowlist(CHAT_PROXY_PATH_PREFIXES_ENV, proxy_paths.prefixes)

    server = AgentServer("ResponsesAgent", enable_chat_proxy=True)
    server.proxy_client = Latin1HeaderClient(timeout=server.chat_proxy_timeout)
    app = server.app
    register_chat_socket_route(app, server.chat_app_port)
    # start_app sets API_PROXY only when it runs the chat UI.
    ui_port = int(server.chat_app_port) if env.read_optional(env.API_PROXY) else None
    register_health_route(app, ui_port)

    if env.read_bool(env.ENABLE_MCP_SERVER):
        from payloads.agent.runtime.mcp_adapter import register_mcp_routes

        register_mcp_routes(app)

    if env.read_bool(env.ENABLE_A2A):
        from payloads.agent.runtime.a2a_adapter import register_a2a_routes

        register_a2a_routes(app)

    if env.read_bool(env.ENABLE_RESOURCES_SIDEBAR):
        register_sidebar_routes(app)

    for extension in extensions:
        extension.register_routes(app)

    from payloads.agent.runtime.startup_prewarm import register_startup_prewarm

    register_startup_prewarm(app)

    import uvicorn

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        reload=args.reload,
        workers=args.workers,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
