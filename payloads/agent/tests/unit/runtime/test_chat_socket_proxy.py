"""Tests for payloads/agent/runtime/chat_socket_proxy.py -- the chat WebSocket pass-through."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from dataclasses import dataclass, field

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from websockets.datastructures import Headers
from websockets.http11 import Request, Response
from websockets.sync.server import Server, ServerConnection, serve

from payloads.agent.runtime.chat_socket_proxy import CHAT_SOCKET_PATH, register_chat_socket_route

DISPLAY_NAME = "Café User"


@dataclass
class FakeChatServer:
    """Stands in for the Node chat UI server: echoes frames, records handshakes."""

    refuse: bool = False
    handshakes: list[Headers] = field(default_factory=list)

    def check_handshake(self, connection: ServerConnection, request: Request) -> Response | None:
        self.handshakes.append(request.headers)
        return connection.respond(403, "Forbidden\n") if self.refuse else None

    def echo(self, connection: ServerConnection) -> None:
        for message in connection:
            connection.send(f"echo:{message!s}")


@pytest.fixture
def fake_chat_server() -> Iterator[tuple[FakeChatServer, str]]:
    fake = FakeChatServer()
    server: Server = serve(fake.echo, "localhost", 0, process_request=fake.check_handshake)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield fake, str(server.socket.getsockname()[1])
    finally:
        server.shutdown()
        thread.join(timeout=5)


def _client(port: str) -> TestClient:
    app = FastAPI()
    register_chat_socket_route(app, port)
    return TestClient(app)


class TestChatSocketRelay:
    def test_relays_frames_both_ways(self, fake_chat_server: tuple[FakeChatServer, str]) -> None:
        _, port = fake_chat_server

        with _client(port).websocket_connect(CHAT_SOCKET_PATH) as socket:
            socket.send_text('{"type":"resume","chatId":"c1"}')

            assert socket.receive_text() == 'echo:{"type":"resume","chatId":"c1"}'

    def test_forwards_identity_headers_byte_for_byte(self, fake_chat_server: tuple[FakeChatServer, str]) -> None:
        fake, port = fake_chat_server
        headers = {
            "x-forwarded-preferred-username": DISPLAY_NAME.encode(),
            "origin": b"https://app.example",
        }

        with _client(port).websocket_connect(CHAT_SOCKET_PATH, headers=headers):
            pass

        forwarded = fake.handshakes[0]
        # The websockets parser surrogate-escapes non-ASCII bytes; undo it to compare wire bytes.
        wire_value = forwarded["x-forwarded-preferred-username"].encode("ascii", "surrogateescape")
        assert wire_value == DISPLAY_NAME.encode()
        assert forwarded["origin"] == "https://app.example"
        assert forwarded["x-forwarded-host"] == "testserver"
        assert forwarded["host"] == f"localhost:{port}"

    def test_keeps_an_existing_forwarded_host(self, fake_chat_server: tuple[FakeChatServer, str]) -> None:
        fake, port = fake_chat_server

        with _client(port).websocket_connect(CHAT_SOCKET_PATH, headers={"x-forwarded-host": "app.example"}):
            pass

        assert fake.handshakes[0].get_all("x-forwarded-host") == ["app.example"]

    def test_refused_upstream_refuses_the_browser(self, fake_chat_server: tuple[FakeChatServer, str]) -> None:
        fake, port = fake_chat_server
        fake.refuse = True

        with pytest.raises(WebSocketDisconnect) as refused, _client(port).websocket_connect(CHAT_SOCKET_PATH):
            pass

        assert refused.value.code == 1008

    def test_unreachable_upstream_refuses_the_browser(self) -> None:
        with pytest.raises(WebSocketDisconnect), _client("1").websocket_connect(CHAT_SOCKET_PATH):
            pass
