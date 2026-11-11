"""Tests for ``payloads/agent/deploy/mcp/inspect.py`` (issue #107).

Covers the *pure* parts: rendering and path resolution. The live
``discover()`` call is mocked end-to-end so no network is required;
real-server inspection is gated behind the ``--run-live``
pytest marker (see issue #107 for the test fixture convention).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from agent_foundry.contracts.mcp import (
    ExternalServerConfig,
    ManagedFunctionsServerConfig,
    ManagedSqlServerConfig,
    PromptsConfig,
    ResourcesConfig,
    ToolsConfig,
)
from payloads.agent.deploy.mcp.inspect import (
    Discovery,
    _resolve_server_dir,
    _server_kind_summary,
    render,
)


def _tool(name: str, description: str = "", **annotations: Any) -> SimpleNamespace:
    """Build a tool stand-in with the same duck-typed shape as ``mcp.types.Tool``."""
    ann_obj = SimpleNamespace(**annotations) if annotations else None
    return SimpleNamespace(name=name, description=description, annotations=ann_obj)


def _prompt(name: str, description: str = "") -> SimpleNamespace:
    return SimpleNamespace(name=name, description=description)


def _resource(uri: str, description: str = "") -> SimpleNamespace:
    return SimpleNamespace(uri=uri, description=description)


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


class TestPathResolution:
    def test_resolves_under_instance_mcp_servers(self, tmp_path: Path) -> None:
        # Default resolution (no dirs on disk) returns the canonical
        # payload-owned location under payloads/agent/ (issue #338).
        out = _resolve_server_dir("demo", "github", tmp_path)
        assert out == tmp_path / "instances" / "demo" / "payloads" / "agent" / "mcp_servers" / "github"


# ---------------------------------------------------------------------------
# Server kind summary (header parenthetical)
# ---------------------------------------------------------------------------


class TestServerKindSummary:
    def test_managed_functions(self) -> None:
        cfg = ManagedFunctionsServerConfig(
            catalog="cat",
            schema="sch",
            tools=ToolsConfig(allow=["x"]),
        )
        assert _server_kind_summary(cfg) == "managed_functions via cat.sch"

    def test_external(self) -> None:
        cfg = ExternalServerConfig(
            connection_name="ghx",
            tools=ToolsConfig(allow=["x"]),
        )
        assert _server_kind_summary(cfg) == "external via ghx"

    def test_managed_sql_no_qualifier(self) -> None:
        cfg = ManagedSqlServerConfig(tools=ToolsConfig(allow=["x"]))
        assert _server_kind_summary(cfg) == "managed_sql"


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


class TestRender:
    def test_marks_allowlisted_tools_with_check(self) -> None:
        cfg = ExternalServerConfig(
            connection_name="ghx",
            tools=ToolsConfig(allow=["search_repos"]),
        )
        discovery = Discovery(
            tools=[
                _tool("search_repos", "Search repositories", readOnlyHint=True),
                _tool("create_issue", "Open an issue", destructiveHint=True),
            ]
        )
        out = render("github", cfg, "https://h/api/2.0/mcp/external/ghx", discovery)
        # ASCII glyphs only -- no Unicode -- per spec.
        assert "[x] search_repos" in out
        assert "[ ] create_issue" in out
        # Annotation tags surfaced in brackets.
        assert "[readOnlyHint]" in out
        assert "[destructiveHint]" in out
        # Counts in the header line.
        assert "Tools (2 discovered, 1 in tools.allow)" in out

    def test_prompts_section_disabled_default(self) -> None:
        cfg = ManagedSqlServerConfig(
            tools=ToolsConfig(allow=["q"]),
            prompts=PromptsConfig(enabled=False),
        )
        discovery = Discovery(
            prompts=[
                _prompt("usage_guide", "How to use this server"),
            ]
        )
        out = render("sql", cfg, "u", discovery)
        assert "Prompts (1 discovered, prompts.enabled=false)" in out
        assert "set prompts.enabled: true" in out

    def test_prompts_allow_star_marks_all(self) -> None:
        cfg = ManagedSqlServerConfig(
            tools=ToolsConfig(allow=["q"]),
            prompts=PromptsConfig(enabled=True, allow=["*"]),
        )
        discovery = Discovery(prompts=[_prompt("a"), _prompt("b")])
        out = render("sql", cfg, "u", discovery)
        assert "[x] a" in out
        assert "[x] b" in out

    def test_resources_section_empty(self) -> None:
        cfg = ManagedSqlServerConfig(
            tools=ToolsConfig(allow=["q"]),
            resources=ResourcesConfig(enabled=False),
        )
        discovery = Discovery(resources=[])
        out = render("sql", cfg, "u", discovery)
        assert "Resources (0 discovered, resources.enabled=false)" in out
        assert "(none)" in out

    def test_error_per_primitive_surfaces_in_section(self) -> None:
        cfg = ManagedSqlServerConfig(tools=ToolsConfig(allow=["q"]))
        discovery = Discovery(errors={"prompts": "RuntimeError: nope"})
        out = render("sql", cfg, "u", discovery)
        assert "Prompts (ERROR: RuntimeError: nope)" in out

    def test_long_descriptions_truncated(self) -> None:
        cfg = ManagedSqlServerConfig(tools=ToolsConfig(allow=["q"]))
        long_desc = "x " * 100
        discovery = Discovery(tools=[_tool("t", long_desc)])
        out = render("sql", cfg, "u", discovery)
        # 60-char window plus "..." suffix = 60 chars max.
        line = next(line for line in out.splitlines() if " t " in line)
        assert line.endswith("...")
