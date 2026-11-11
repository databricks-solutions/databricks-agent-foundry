"""Tests for payloads/agent/runtime/health.py -- ``/health`` gated on the chat UI."""

from __future__ import annotations

import socket
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from payloads.agent.runtime.health import HEALTH_PATH, register_health_route


def _app_with_default_health(chat_app_port: int | None) -> FastAPI:
    app = FastAPI()

    @app.get(HEALTH_PATH)
    def always_healthy() -> dict[str, str]:
        return {"status": "healthy"}

    register_health_route(app, chat_app_port)
    return app


@pytest.fixture
def listening_port() -> Iterator[int]:
    with socket.socket() as listener:
        listener.bind(("localhost", 0))
        listener.listen()
        yield int(listener.getsockname()[1])


@pytest.fixture
def closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("localhost", 0))
        return int(probe.getsockname()[1])


def test_healthy_without_chat_ui() -> None:
    response = TestClient(_app_with_default_health(None)).get(HEALTH_PATH)
    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}


def test_starting_while_chat_ui_not_listening(closed_port: int) -> None:
    response = TestClient(_app_with_default_health(closed_port)).get(HEALTH_PATH)
    assert response.status_code == 503
    assert response.json()["status"] == "starting"


def test_healthy_once_chat_ui_listens(listening_port: int) -> None:
    response = TestClient(_app_with_default_health(listening_port)).get(HEALTH_PATH)
    assert response.status_code == 200


def test_replaces_the_existing_route() -> None:
    app = _app_with_default_health(None)
    assert [getattr(route, "path", None) for route in app.router.routes].count(HEALTH_PATH) == 1
