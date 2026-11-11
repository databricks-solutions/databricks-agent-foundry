"""Pass-through for the chat WebSocket (#772): browser <-> chat UI server.

MLflow's chat proxy relays plain HTTP only, so this route claims the socket
path and pipes frames to the Node chat UI server unchanged.  Origin and auth
checks stay in Node (``server/src/lib/chat-socket.ts``); a socket Node
refuses is refused here before the browser's handshake completes, so the
client falls back to SSE.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import FastAPI, WebSocket
from starlette.websockets import WebSocketDisconnect, WebSocketState
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake

logger = logging.getLogger(__name__)

CHAT_SOCKET_PATH = "/api/chat/socket"

# Same limit as the Node server's ``maxPayload``.
_MAX_MESSAGE_BYTES = 10 * 1024 * 1024
_POLICY_VIOLATION = 1008

# Hop-by-hop and handshake headers; ``websockets`` writes its own.
_HANDSHAKE_HEADERS = frozenset(
    {
        b"host",
        b"connection",
        b"upgrade",
        b"keep-alive",
        b"te",
        b"transfer-encoding",
        b"content-length",
        b"sec-websocket-key",
        b"sec-websocket-version",
        b"sec-websocket-extensions",
        b"sec-websocket-protocol",
    }
)


def register_chat_socket_route(app: FastAPI, chat_app_port: str) -> None:
    """Relay ``CHAT_SOCKET_PATH`` to the chat UI server on *chat_app_port*."""
    relay = ChatSocketRelay(f"ws://localhost:{chat_app_port}{CHAT_SOCKET_PATH}")
    app.add_api_websocket_route(CHAT_SOCKET_PATH, relay)


class ChatSocketRelay:
    """WebSocket endpoint piping one browser socket to *upstream_url*."""

    def __init__(self, upstream_url: str) -> None:
        self.upstream_url = upstream_url

    async def __call__(self, browser: WebSocket) -> None:
        try:
            upstream = await connect(
                self.upstream_url,
                additional_headers=forwarded_headers(browser),
                user_agent_header=None,
                max_size=_MAX_MESSAGE_BYTES,
            )
        except (OSError, InvalidHandshake, TimeoutError) as exc:
            logger.warning("Chat socket refused by the chat UI server: %s", exc)
            await browser.close(code=_POLICY_VIOLATION)
            return
        await browser.accept()
        async with upstream:
            await _pipe(browser, upstream)
        if browser.client_state == WebSocketState.CONNECTED:
            await browser.close()


def forwarded_headers(browser: WebSocket) -> list[tuple[str, str]]:
    """The browser's headers for the upstream handshake, original bytes intact.

    Starlette decodes header bytes as latin-1 and ``websockets`` encodes
    values as UTF-8, so the raw bytes are decoded as UTF-8 to round-trip a
    display name such as "Café" in ``x-forwarded-preferred-username``.
    """
    headers = [
        (name.decode("latin-1"), value.decode("utf-8", errors="replace"))
        for name, value in browser.headers.raw
        if name.lower() not in _HANDSHAKE_HEADERS
    ]
    host = browser.headers.get("host")
    if host and "x-forwarded-host" not in browser.headers:
        headers.append(("x-forwarded-host", host))
    return headers


async def _pipe(browser: WebSocket, upstream: ClientConnection) -> None:
    tasks = {
        asyncio.create_task(_browser_to_upstream(browser, upstream)),
        asyncio.create_task(_upstream_to_browser(upstream, browser)),
    }
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    for task in done:
        error = task.exception()
        if error is not None and not isinstance(error, (ConnectionClosed, WebSocketDisconnect)):
            logger.warning("Chat socket relay failed: %s", error)


async def _browser_to_upstream(browser: WebSocket, upstream: ClientConnection) -> None:
    async for message in browser.iter_text():
        await upstream.send(message)


async def _upstream_to_browser(upstream: ClientConnection, browser: WebSocket) -> None:
    async for message in upstream:
        if isinstance(message, str):
            await browser.send_text(message)
