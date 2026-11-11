"""Parity test for the shared tool factory (issue #153).

The two call sites in :mod:`payloads.agent.runtime.core` -- ``create_tools``
(agent-level ``TOOLS``) and ``create_skill_tools`` (skill-level
``SKILL_TOOLS``) -- previously carried independent per-type factory
dispatch logic that had drifted in detail.  After issue #153 they both
delegate to :func:`payloads.agent.runtime._tool_factory.create_function_tool`.

This test pins the parity contract: given a typed
:class:`~agent_foundry.contracts.Tool` that contains everything both
call sites need, the underlying ``create_genie_tool`` /
``create_vector_search_tool`` factory is invoked with **identical
arguments** from either entry point.  When the two call sites
intentionally differ (e.g. agent-level falls back to ``variables`` for
missing resource IDs), that difference is exercised separately; this
test asserts the shape of the call when both inputs are equivalent.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from payloads.agent.runtime.tool_builders import create_skill_tools, create_tools


@pytest.fixture
def mock_resolve(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub out the workspace-client resolution both entry points use."""
    monkeypatch.setattr(
        "payloads.agent.runtime.tool_builders.resolve_tools_client",
        lambda ws: ws,
    )


def _genie_dict_for_skill(name: str, space_id: str) -> dict[str, Any]:
    return {
        "name": name,
        "type": "genie",
        "description": "Query a Genie space.",
        "space_id": space_id,
    }


def _genie_dict_for_agent(name: str, space_id: str) -> dict[str, Any]:
    """Same logical content as :func:`_genie_dict_for_skill`.

    Wrapped in its own helper to make it explicit that both call sites
    receive the same payload; if a future refactor diverges the input
    schemas, this helper is the place to express the mapping.
    """
    return {
        "name": name,
        "type": "genie",
        "description": "Query a Genie space.",
        "space_id": space_id,
    }


def _vs_dict_for_skill(name: str, index_name: str) -> dict[str, Any]:
    return {
        "name": name,
        "type": "vector_search",
        "description": "Search the documentation index.",
        "index_name": index_name,
        "columns": ["id", "content"],
        "query_type": "HYBRID",
        "num_results": 5,
        "score_threshold": 0.6,
    }


def _vs_dict_for_agent(name: str, index_name: str) -> dict[str, Any]:
    return _vs_dict_for_skill(name, index_name)


def test_genie_call_args_match_across_entry_points(
    mock_resolve: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both ``create_tools`` and ``create_skill_tools`` invoke
    ``create_genie_tool`` with identical positional + keyword args
    when given equivalent typed input.
    """
    captured: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def _spy(*args: Any, **kwargs: Any) -> Any:
        captured.append((args[1:], kwargs))  # drop ws (positional 0)
        return SimpleNamespace(name=kwargs.get("name", "query_genie"))

    monkeypatch.setattr("payloads.agent.runtime._tool_factory.create_genie_tool", _spy)

    ws = MagicMock()
    skill_dict = _genie_dict_for_skill("query_finance", "space-finance-1")
    agent_dict = _genie_dict_for_agent("query_finance", "space-finance-1")

    create_skill_tools(ws, {"SKILL_TOOLS": [skill_dict]}, {})
    create_tools(ws, {"TOOLS": [agent_dict]}, {})

    assert len(captured) == 2, "expected two invocations, one per entry point"
    skill_call, agent_call = captured
    assert skill_call == agent_call, (
        f"genie factory called with different args:\n  skill_tools: {skill_call}\n  create_tools: {agent_call}"
    )


def test_vector_search_call_args_match_across_entry_points(
    mock_resolve: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same parity contract for the vector_search factory.  Exercises
    the optional kwargs surface (``columns`` / ``query_type`` /
    ``num_results`` / ``score_threshold``) that previously had two
    independent implementations.
    """
    captured: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def _spy(*args: Any, **kwargs: Any) -> Any:
        captured.append((args[1:], kwargs))
        return SimpleNamespace(name=kwargs.get("name", "query_vector_search"))

    monkeypatch.setattr(
        "payloads.agent.runtime._tool_factory.create_vector_search_tool",
        _spy,
    )

    ws = MagicMock()
    skill_dict = _vs_dict_for_skill("docs_search", "cat.sch.docs_index")
    agent_dict = _vs_dict_for_agent("docs_search", "cat.sch.docs_index")

    create_skill_tools(ws, {"SKILL_TOOLS": [skill_dict]}, {})
    create_tools(ws, {"TOOLS": [agent_dict]}, {})

    assert len(captured) == 2
    skill_call, agent_call = captured
    assert skill_call == agent_call, (
        f"vector_search factory called with different args:\n  skill_tools: {skill_call}\n  create_tools: {agent_call}"
    )


def test_mcp_served_tools_skipped_from_both_entry_points(
    mock_resolve: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``uc_function`` and ``mcp`` tools are owned by the bricks/mcp/
    runtime path; both entry points must skip them silently.
    """
    fail = MagicMock(side_effect=AssertionError("must not be called"))
    monkeypatch.setattr("payloads.agent.runtime._tool_factory.create_genie_tool", fail)
    monkeypatch.setattr("payloads.agent.runtime._tool_factory.create_vector_search_tool", fail)

    ws = MagicMock()
    mcp_dict = {
        "name": "search_issues",
        "type": "mcp",
        "description": "MCP-served tool.",
    }
    uc_dict = {
        "name": "get_tickets",
        "type": "uc_function",
        "description": "UC function tool.",
        "function_name": "cat.sch.get_tickets",
    }

    skill_result = create_skill_tools(
        ws,
        {"SKILL_TOOLS": [mcp_dict, uc_dict]},
        {},
    )
    agent_result = create_tools(
        ws,
        {"TOOLS": [mcp_dict, uc_dict]},
        {},
    )

    assert skill_result == [] == agent_result
