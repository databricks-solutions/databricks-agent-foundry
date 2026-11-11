"""Tests for payloads/agent/runtime/mcp_adapter.py -- MCP tool server."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestAskAgentTool:
    @pytest.mark.asyncio
    @patch("payloads.agent.runtime.mcp_adapter.run_agent", new_callable=AsyncMock)
    async def test_returns_agent_output(self, mock_run: MagicMock) -> None:
        mock_run.return_value = "Hello from the agent"
        from payloads.agent.runtime.mcp_adapter import ask_agent

        result = await ask_agent("test question")
        assert result == "Hello from the agent"
        mock_run.assert_awaited_once_with("test question", source="mcp")


class TestRegisterMcpRoutes:
    def test_registers_route_on_fastapi_app(self) -> None:
        from fastapi import FastAPI

        from payloads.agent.runtime.mcp_adapter import register_mcp_routes

        app = FastAPI()
        initial_count = len(app.routes)
        register_mcp_routes(app)
        assert len(app.routes) > initial_count
