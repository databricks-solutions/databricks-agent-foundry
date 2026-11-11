"""Tests for :mod:`payloads.agent.runtime.request_context`."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from payloads.agent.runtime.request_context import (
    _decode_header_value,
    get_request_username,
    get_workspace_client,
    user_workspace_client,
)


class TestHeaderDecoding:
    """Names go into the prompt, so their bytes have to survive the hop.

    HTTP header values are latin-1 by specification: an OBO username with
    anything outside ASCII arrives mangled, and lands in the prompt that way
    -- observed in production as "CafÃ© User".
    """

    def test_latin1_mangled_utf8_is_recovered(self) -> None:
        assert _decode_header_value("CafÃ© User") == "Café User"

    def test_ascii_passes_through(self) -> None:
        assert _decode_header_value("dave@example.com") == "dave@example.com"

    def test_undecodable_values_are_left_alone(self) -> None:
        """Better an odd name than an exception on the request path."""
        assert _decode_header_value("Café") == "Café"

    def test_username_is_decoded_at_the_source(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "payloads.agent.runtime.request_context._get_all_request_headers",
            lambda: {"x-forwarded-preferred-username": "CafÃ© User"},
        )

        assert get_request_username() == "Café User"


class TestGetWorkspaceClient:
    def test_environment_client_is_reused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)
        with patch(
            "payloads.agent.runtime.request_context.WorkspaceClient", side_effect=lambda **_: MagicMock()
        ) as ctor:
            first = get_workspace_client()
            second = get_workspace_client()

        assert first is second
        ctor.assert_called_once()

    def test_profile_client_is_not_reused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DATABRICKS_CONFIG_PROFILE", "dev")
        fake = MagicMock()
        fake.config.authenticate.return_value = {}
        with patch("payloads.agent.runtime.request_context.WorkspaceClient", return_value=fake) as ctor:
            get_workspace_client()
            get_workspace_client()

        assert ctor.call_count == 2

    def test_user_client_carries_the_product_user_agent(self) -> None:
        product = {"product": "agent-foundry", "product_version": "1.2.3"}
        with (
            patch("payloads.agent.runtime.request_context.product_kwargs", return_value=product),
            patch("payloads.agent.runtime.request_context.WorkspaceClient") as ctor,
        ):
            user_workspace_client("obo-token")

        ctor.assert_called_once_with(token="obo-token", auth_type="pat", **product)
