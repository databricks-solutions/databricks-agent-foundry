"""``/health`` that reports ready only once the chat UI server listens.

Memory, plans and history search are served by the chat UI server; until it
listens, those tools fail, so the App is not ready even though the backend answers.
"""

from __future__ import annotations

import asyncio
from typing import Final

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

HEALTH_PATH: Final = "/health"
_CONNECT_TIMEOUT_SECONDS: Final = 2.0


def register_health_route(app: FastAPI, chat_app_port: int | None) -> None:
    """Replace AgentServer's always-healthy ``/health``; *chat_app_port* ``None`` means no chat UI."""
    app.router.routes[:] = [route for route in app.router.routes if getattr(route, "path", None) != HEALTH_PATH]
    app.state.health_chat_app_port = chat_app_port
    app.add_api_route(HEALTH_PATH, health, methods=["GET"])


async def health(request: Request) -> JSONResponse:
    port: int | None = request.app.state.health_chat_app_port
    if port is None or await _listens(port):
        return JSONResponse({"status": "healthy"})
    return JSONResponse({"status": "starting", "detail": "chat UI is not listening yet"}, status_code=503)


async def _listens(port: int) -> bool:
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection("localhost", port), _CONNECT_TIMEOUT_SECONDS)
    except (OSError, TimeoutError):
        return False
    writer.close()
    await writer.wait_closed()
    return True
