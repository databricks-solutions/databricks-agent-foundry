"""Tests for payloads/agent/runtime/tool_assembly.py -- tool list assembly."""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_foundry._instance_layout import resolve_payload_subdir
from agent_foundry.contracts.skills import SKILL_FILE, render_skill_md
from payloads.agent.runtime.tool_assembly import build_all_tools


class TestBuildAllToolsLazyMode:
    """Wiring contract: ``_load_skill`` is appended only when the agent
    is in lazy mode AND the build-time index resolved to at least one
    body file on disk.
    """

    @staticmethod
    def _write_skill(skills_root: Path, skill_id: str) -> None:
        d = skills_root / skill_id
        d.mkdir(parents=True, exist_ok=True)
        (d / SKILL_FILE).write_text(render_skill_md(skill_id, "Use when testing.", "body"), encoding="utf-8")

    @patch("payloads.agent.runtime.tool_assembly.create_skill_tools", return_value=[])
    @patch("payloads.agent.runtime.tool_assembly.create_tools", return_value=[])
    @patch("payloads.agent.runtime.tool_assembly.resolve_instance_dir")
    def test_appends_load_skill_in_lazy_mode(
        self, mock_resolve: MagicMock, _mock_tools: MagicMock, _mock_skill_tools: MagicMock, tmp_path: Path
    ) -> None:
        mock_resolve.return_value = tmp_path
        # The runtime reads skill bodies from the payload-owned skills
        # subdir (``payloads/agent/skills/``), not a bare ``skills/`` dir.
        self._write_skill(resolve_payload_subdir(tmp_path, "skills"), "alpha")
        config = {
            "SKILL_LOADING": "lazy",
            "SKILL_INDEX": {"alpha": "alpha/SKILL.md"},
        }
        tools = build_all_tools(MagicMock(), config, {})
        assert any(t.name == "_load_skill" for t in tools)

    @patch("payloads.agent.runtime.tool_assembly.create_skill_tools", return_value=[])
    @patch("payloads.agent.runtime.tool_assembly.create_tools", return_value=[])
    @patch("payloads.agent.runtime.tool_assembly.resolve_instance_dir")
    def test_omits_load_skill_in_eager_mode(
        self, mock_resolve: MagicMock, _mock_tools: MagicMock, _mock_skill_tools: MagicMock, tmp_path: Path
    ) -> None:
        mock_resolve.return_value = tmp_path
        self._write_skill(tmp_path / "skills", "alpha")
        # Even with a populated index, eager mode must not register the
        # built-in tool.
        config = {
            "SKILL_LOADING": "eager",
            "SKILL_INDEX": {"alpha": "alpha/SKILL.md"},
        }
        tools = build_all_tools(MagicMock(), config, {})
        assert not any(t.name == "_load_skill" for t in tools)

    @patch("payloads.agent.runtime.tool_assembly.create_skill_tools", return_value=[])
    @patch("payloads.agent.runtime.tool_assembly.create_tools", return_value=[])
    @patch("payloads.agent.runtime.tool_assembly.resolve_instance_dir")
    def test_omits_load_skill_when_index_empty(
        self, mock_resolve: MagicMock, _mock_tools: MagicMock, _mock_skill_tools: MagicMock, tmp_path: Path
    ) -> None:
        mock_resolve.return_value = tmp_path
        config = {"SKILL_LOADING": "lazy", "SKILL_INDEX": {}}
        tools = build_all_tools(MagicMock(), config, {})
        assert not any(t.name == "_load_skill" for t in tools)

    @patch("payloads.agent.runtime.tool_assembly.create_skill_tools", return_value=[])
    @patch("payloads.agent.runtime.tool_assembly.create_tools", return_value=[])
    @patch("payloads.agent.runtime.tool_assembly.resolve_instance_dir", return_value=None)
    def test_skips_when_instance_dir_unresolved(
        self, _mock_resolve: MagicMock, _mock_tools: MagicMock, _mock_skill_tools: MagicMock
    ) -> None:
        config = {
            "SKILL_LOADING": "lazy",
            "SKILL_INDEX": {"alpha": "alpha/SKILL.md"},
        }
        tools = build_all_tools(MagicMock(), config, {})
        # Without an instance_dir the helper has nowhere to read body
        # files from, so it must short-circuit instead of crashing.
        assert tools == []


class TestBuildAllToolsMcpOnly:
    """``MCP_ONLY=True`` (the framework default) suppresses the direct
    tool factories.  ``MCP_ONLY=False`` preserves the legacy behaviour
    for instances that haven't migrated to MCP-served tools yet.
    """

    @patch("payloads.agent.runtime.tool_assembly.create_skill_tools")
    @patch("payloads.agent.runtime.tool_assembly.create_tools")
    @patch("payloads.agent.runtime.tool_assembly.resolve_instance_dir", return_value=None)
    def test_mcp_only_skips_direct_factories(
        self, _mock_resolve: MagicMock, mock_create_tools: MagicMock, mock_create_skill_tools: MagicMock
    ) -> None:
        config = {"MCP_ONLY": True, "TOOLS": [], "SKILL_TOOLS": []}
        tools = build_all_tools(MagicMock(), config, {})
        assert tools == []
        mock_create_tools.assert_not_called()
        mock_create_skill_tools.assert_not_called()

    @patch("payloads.agent.runtime.tool_assembly.create_skill_tools", return_value=[])
    @patch("payloads.agent.runtime.tool_assembly.create_tools", return_value=[])
    @patch("payloads.agent.runtime.tool_assembly.resolve_instance_dir", return_value=None)
    def test_mcp_only_default_true_when_unset(
        self, _mock_resolve: MagicMock, mock_create_tools: MagicMock, mock_create_skill_tools: MagicMock
    ) -> None:
        """A config dict missing ``MCP_ONLY`` must default to True so
        upstream callers that haven't surfaced the flag stay on the
        MCP-only path."""
        tools = build_all_tools(MagicMock(), {}, {})
        assert tools == []
        mock_create_tools.assert_not_called()
        mock_create_skill_tools.assert_not_called()

    @patch("payloads.agent.runtime.tool_assembly.create_skill_tools", return_value=[])
    @patch("payloads.agent.runtime.tool_assembly.create_tools", return_value=[])
    @patch("payloads.agent.runtime.tool_assembly.resolve_instance_dir", return_value=None)
    def test_mcp_only_false_invokes_direct_factories(
        self, _mock_resolve: MagicMock, mock_create_tools: MagicMock, mock_create_skill_tools: MagicMock
    ) -> None:
        config = {"MCP_ONLY": False}
        build_all_tools(MagicMock(), config, {})
        mock_create_tools.assert_called_once()
        mock_create_skill_tools.assert_called_once()

    @patch("payloads.agent.runtime.tool_assembly.resolve_instance_dir", return_value=None)
    def test_mcp_only_logs_warning_per_dropped_tool(
        self, _mock_resolve: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Each direct-mode declaration that gets suppressed must produce
        a discoverable WARNING so deployers don't silently lose tools."""
        config = {
            "MCP_ONLY": True,
            "TOOLS": [{"name": "ask_genie", "type": "genie"}],
            "SKILL_TOOLS": [
                {"name": "search_docs", "type": "vector_search"},
                # MCP-served types must NOT trigger a warning -- they're
                # routed through the MCP path by the assembler.
                {"name": "uc_lookup", "type": "uc_function"},
            ],
        }
        with caplog.at_level(logging.WARNING):
            build_all_tools(MagicMock(), config, {})

        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert any("ask_genie" in m and "genie" in m for m in warnings)
        assert any("search_docs" in m and "vector_search" in m for m in warnings)
        assert not any("uc_lookup" in m for m in warnings)
