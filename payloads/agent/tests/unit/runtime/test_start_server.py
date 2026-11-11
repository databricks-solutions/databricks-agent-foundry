"""Tests for payloads/agent/runtime/start_server.py -- chat-proxy header forwarding, sidebar routes."""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from payloads.agent.runtime.resources_routes import RESOURCES_PATH
from payloads.agent.runtime.skill_authoring_routes import SKILLS_PATH
from payloads.agent.runtime.start_server import Latin1HeaderClient, _extend_allowlist, register_sidebar_routes

MANGLED_NAME = "CafÃ© User"


class TestLatin1HeaderClient:
    def test_stock_client_rejects_a_mangled_display_name(self) -> None:
        """The failure the subclass exists for: the chat proxy's 502."""
        with pytest.raises(UnicodeEncodeError):
            httpx.AsyncClient().build_request(
                "GET", "http://localhost:3000/", headers={"x-forwarded-preferred-username": MANGLED_NAME}
            )

    def test_forwards_the_original_utf8_bytes(self) -> None:
        request = Latin1HeaderClient().build_request(
            "GET", "http://localhost:3000/", headers={"x-forwarded-preferred-username": MANGLED_NAME}
        )

        assert (b"x-forwarded-preferred-username", "Café User".encode()) in request.headers.raw

    def test_ascii_headers_are_unchanged(self) -> None:
        request = Latin1HeaderClient().build_request(
            "GET", "http://localhost:3000/", headers={"x-forwarded-access-token": "tok123"}
        )

        assert request.headers["x-forwarded-access-token"] == "tok123"


class TestExtendAllowlist:
    def test_keeps_existing_entries_and_appends_new_ones_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CHAT_PROXY_ALLOWED_EXACT_PATHS", "/existing, /cockpit")

        _extend_allowlist("CHAT_PROXY_ALLOWED_EXACT_PATHS", ("/cockpit", "/runs"))

        import os

        assert os.environ["CHAT_PROXY_ALLOWED_EXACT_PATHS"] == "/existing,/cockpit,/runs"

    def test_leaves_unset_variable_unset_when_nothing_to_add(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CHAT_PROXY_ALLOWED_PATH_PREFIXES", raising=False)

        _extend_allowlist("CHAT_PROXY_ALLOWED_PATH_PREFIXES", ())

        import os

        assert "CHAT_PROXY_ALLOWED_PATH_PREFIXES" not in os.environ


class TestSidebarRoutes:
    @pytest.mark.parametrize(("user_skills", "claims_skill_writes"), [("false", True), ("true", False)])
    def test_skill_writes_go_to_the_chat_ui_with_lakebase_user_skills(
        self, monkeypatch: pytest.MonkeyPatch, user_skills: str, claims_skill_writes: bool
    ) -> None:
        monkeypatch.setenv("ENABLE_USER_SKILLS", user_skills)
        app = FastAPI()

        register_sidebar_routes(app)

        paths = {getattr(route, "path", "") for route in app.routes}
        assert RESOURCES_PATH in paths
        assert (SKILLS_PATH in paths) is claims_skill_writes
