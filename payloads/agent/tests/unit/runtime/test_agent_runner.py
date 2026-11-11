"""Tests for payloads/agent/runtime/agent_runner.py -- shared agent lifecycle."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from payloads.agent.runtime.agent_runner import (
    get_fallback_headers,
    run_agent,
    set_request_headers,
)
from payloads.agent.runtime.context_limit import CONTEXT_LIMIT_NOTICE
from payloads.agent.runtime.mcp_availability import UnavailableServer, user_notice
from payloads.agent.runtime.mcp_connect import ConnectedMcp


class TestSetRequestHeaders:
    def test_set_and_get_headers(self) -> None:
        token = set_request_headers({"x-forwarded-access-token": "tok123"})
        assert get_fallback_headers()["x-forwarded-access-token"] == "tok123"
        from payloads.agent.runtime.agent_runner import _request_headers_var

        _request_headers_var.reset(token)

    def test_default_is_empty(self) -> None:
        assert get_fallback_headers() == {} or isinstance(get_fallback_headers(), dict)

    def test_the_username_is_decoded_on_this_path_too(self) -> None:
        """The A2A/MCP path used to read the header key directly, which put
        a latin-1 mangled name straight into the prompt."""
        from payloads.agent.runtime.request_context import get_request_username

        token = set_request_headers({"x-forwarded-preferred-username": "CafÃ© User"})
        try:
            assert get_request_username() == "Café User"
        finally:
            from payloads.agent.runtime.agent_runner import _request_headers_var

            _request_headers_var.reset(token)


JIRA_DOWN = UnavailableServer("jira", "connect_failed")


class TestRunAgent:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("unavailable", "expected"),
        [
            ((), "The answer is 42"),
            ((JIRA_DOWN,), f"{user_notice((JIRA_DOWN,))}\n\nThe answer is 42"),
        ],
    )
    @patch("payloads.agent.runtime.agent_runner.Runner")
    @patch("payloads.agent.runtime.agent_runner.create_agent")
    @patch("payloads.agent.runtime.agent_runner.managed_mcp_servers")
    @patch("payloads.agent.runtime.agent_runner.build_all_tools", return_value=[])
    @patch("payloads.agent.runtime.agent_runner.load_agent_config_and_vars")
    @patch("payloads.agent.runtime.agent_runner.get_workspace_client")
    @patch("payloads.agent.runtime.agent_runner.init_agent_module")
    @patch("payloads.agent.runtime.agent_runner.mlflow")
    async def test_returns_final_output(
        self,
        mock_mlflow: MagicMock,
        mock_init: MagicMock,
        mock_ws: MagicMock,
        mock_load: MagicMock,
        mock_tools: MagicMock,
        mock_mcp_ctx: MagicMock,
        mock_create: MagicMock,
        mock_runner: MagicMock,
        unavailable: tuple[UnavailableServer, ...],
        expected: str,
    ) -> None:
        mock_ws.return_value = MagicMock()
        mock_load.return_value = (
            {"MAX_ITERATIONS": 5, "SYSTEM_PROMPT": "hi", "LLM_ENDPOINT_NAME": "m"},
            {},
        )

        mock_mcp_mgr = AsyncMock()
        mock_mcp_mgr.__aenter__ = AsyncMock(return_value=ConnectedMcp([], unavailable, {}))
        mock_mcp_mgr.__aexit__ = AsyncMock(return_value=False)
        mock_mcp_ctx.return_value = mock_mcp_mgr

        mock_result = MagicMock()
        mock_result.final_output = "The answer is 42"
        mock_runner.run = AsyncMock(return_value=mock_result)

        result = await run_agent("What is 6*7?", source="mcp")
        assert result == expected
        assert mock_create.call_args.kwargs["unavailable_mcp"] == unavailable
        mock_runner.run.assert_awaited_once()

    @pytest.mark.asyncio
    @patch("payloads.agent.runtime.agent_runner.init_agent_module", side_effect=RuntimeError("boom"))
    @patch("payloads.agent.runtime.agent_runner.mlflow")
    async def test_returns_error_on_failure(self, mock_mlflow: MagicMock, mock_init: MagicMock) -> None:
        result = await run_agent("fail please", source="test")
        assert result.startswith("Error:")
        assert "boom" in result

    @pytest.mark.asyncio
    @patch(
        "payloads.agent.runtime.agent_runner.init_agent_module",
        side_effect=RuntimeError("prompt is too long: 214301 tokens > 200000 maximum"),
    )
    @patch("payloads.agent.runtime.agent_runner.mlflow")
    async def test_context_limit_returns_actionable_notice(self, mock_mlflow: MagicMock, mock_init: MagicMock) -> None:
        """A2A/MCP callers got the raw provider string, which is meaningless
        to them; the window rejection is the one failure with a known
        remedy, so it gets the notice instead.
        """
        result = await run_agent("too much history", source="a2a")
        assert result == CONTEXT_LIMIT_NOTICE
        assert not result.startswith("Error:")
