"""Tests for the deprecated ``skill.yml`` / ``domain.yml`` format, read for one more release.

Delete this file together with ``payloads/agent/deploy/skills/_legacy.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from agent_foundry._instance_layout import resolve_payload_subdir
from agent_foundry.contracts import GenieTool, Tool, VectorSearchTool
from payloads.agent.deploy.skills._legacy import (
    _normalize_tool_manifest,
    _resolve_skill_tool_refs,
    collect_mcp_brick_tool_names,
)
from payloads.agent.deploy.skills.assemble import assemble_skills, load_skill
from payloads.agent.deploy.skills.validate import (
    validate_domain_dir,
    validate_instance_skills,
    validate_skill_dir,
)
from payloads.agent.tests.unit.deploy.skills.conftest import create_skill, set_lazy_loading

_MIN_PROMPT = (
    "# test\n"
    "## When to activate\ntest\n"
    "## Workflow\n1\n"
    "## Tool call examples\n```\n```\n"
    "## Output format\ntest\n"
    "## Edge cases\ntest\n"
)
_MIN_TOOLS = "tools:\n  - name: t\n    type: uc_function\n    function_name: cat.sch.t\n    description: test\n"


def _make_minimal_skill(skill_dir: Path, tools_yml_text: str) -> None:
    """Create a minimal valid skill (skill.yml + prompt.md + tools.yml)."""
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "skill.yml").write_text(
        yaml.dump(
            {
                "name": "test",
                "description": "test",
                "triggers": ["t"],
                "tools": ["t"],
            }
        )
    )
    (skill_dir / "prompt.md").write_text(_MIN_PROMPT)
    (skill_dir / "tools.yml").write_text(tools_yml_text)


# ---------------------------------------------------------------------------
# Skill Domain validation
# ---------------------------------------------------------------------------


class TestDomainYmlValidation:
    """Verify domain.yml validation rules."""

    def test_valid_domain_passes(self, legacy_instance_dir: Path) -> None:
        domain_dir = resolve_payload_subdir(legacy_instance_dir, "skills") / "test_domain"
        errors = validate_domain_dir(domain_dir)
        error_msgs = [e for e in errors if e.severity == "error"]
        assert len(error_msgs) == 0

    def test_missing_domain_yml(self, tmp_path: Path) -> None:
        domain_dir = tmp_path / "bad_domain"
        domain_dir.mkdir()
        errors = validate_domain_dir(domain_dir)
        assert any("domain.yml" in e.path and "missing" in e.message for e in errors)

    def test_missing_name(self, tmp_path: Path) -> None:
        domain_dir = tmp_path / "no_name_domain"
        domain_dir.mkdir()
        (domain_dir / "domain.yml").write_text(
            yaml.dump(
                {
                    "description": "test domain",
                }
            )
        )
        errors = validate_domain_dir(domain_dir)
        assert any("name" in e.message for e in errors if e.severity == "error")

    def test_missing_description(self, tmp_path: Path) -> None:
        domain_dir = tmp_path / "no_desc_domain"
        domain_dir.mkdir()
        (domain_dir / "domain.yml").write_text(
            yaml.dump(
                {
                    "name": "test",
                }
            )
        )
        errors = validate_domain_dir(domain_dir)
        assert any("description" in e.message for e in errors if e.severity == "error")

    def test_empty_domain_warns_no_skills(self, tmp_path: Path) -> None:
        domain_dir = tmp_path / "empty_domain"
        domain_dir.mkdir()
        (domain_dir / "domain.yml").write_text(
            yaml.dump(
                {
                    "name": "test",
                    "description": "test domain",
                }
            )
        )
        errors = validate_domain_dir(domain_dir)
        assert any("no skill subdirectories" in e.message for e in errors)

    def test_validates_nested_skills(self, legacy_instance_dir: Path) -> None:
        domain_dir = resolve_payload_subdir(legacy_instance_dir, "skills") / "test_domain"
        errors = validate_domain_dir(domain_dir)
        error_paths = [e.path for e in errors]
        for path in error_paths:
            assert "test_domain/" in path

    def test_warns_that_domain_prompt_md_is_no_longer_rendered(self, legacy_instance_dir: Path) -> None:
        domain_dir = resolve_payload_subdir(legacy_instance_dir, "skills") / "test_domain"
        warnings = [e for e in validate_domain_dir(domain_dir) if e.severity == "warning"]
        assert any("domain.yml" in e.path and "deprecated" in e.message for e in warnings)
        assert any("domain_prompt.md" in e.path and "system_prompt" in e.message for e in warnings)


# ---------------------------------------------------------------------------
# Skill validation
# ---------------------------------------------------------------------------


class TestSkillYmlValidation:
    """Verify skill.yml validation rules."""

    def test_valid_skill_passes(self, legacy_instance_dir: Path) -> None:
        skill_dir = resolve_payload_subdir(legacy_instance_dir, "skills") / "test_domain" / "alpha_skill"
        errors = validate_skill_dir(skill_dir)
        error_msgs = [e for e in errors if e.severity == "error"]
        assert len(error_msgs) == 0

    def test_warns_that_the_format_is_deprecated(self, legacy_instance_dir: Path) -> None:
        skill_dir = resolve_payload_subdir(legacy_instance_dir, "skills") / "test_domain" / "alpha_skill"
        errors = validate_skill_dir(skill_dir)
        assert any(e.severity == "warning" and "deprecated" in e.message for e in errors)

    def test_prompt_md_without_skill_yml_is_not_a_legacy_skill(self, tmp_path: Path) -> None:
        skill_dir = tmp_path / "bad_skill"
        skill_dir.mkdir()
        (skill_dir / "prompt.md").write_text("# test")
        errors = validate_skill_dir(skill_dir)
        assert [(e.path, e.message) for e in errors] == [("bad_skill/SKILL.md", "file is missing")]

    def test_missing_name(self, tmp_path: Path) -> None:
        skill_dir = tmp_path / "no_name"
        skill_dir.mkdir()
        (skill_dir / "skill.yml").write_text(
            yaml.dump(
                {
                    "description": "test",
                    "triggers": ["t"],
                    "tools": ["t"],
                }
            )
        )
        (skill_dir / "prompt.md").write_text(_MIN_PROMPT)
        (skill_dir / "tools.yml").write_text(_MIN_TOOLS)
        errors = validate_skill_dir(skill_dir)
        assert any("name" in e.message for e in errors if e.severity == "error")

    def test_empty_triggers(self, tmp_path: Path) -> None:
        skill_dir = tmp_path / "empty_triggers"
        skill_dir.mkdir()
        (skill_dir / "skill.yml").write_text(
            yaml.dump(
                {
                    "name": "test",
                    "description": "test",
                    "triggers": [],
                    "tools": ["t"],
                }
            )
        )
        (skill_dir / "prompt.md").write_text(_MIN_PROMPT)
        (skill_dir / "tools.yml").write_text(_MIN_TOOLS)
        errors = validate_skill_dir(skill_dir)
        assert any("triggers" in e.message and "non-empty" in e.message for e in errors)


# ---------------------------------------------------------------------------
# Tool-less (prompt-only) skill validation
# ---------------------------------------------------------------------------
#
# Tool-less skills declare ``tools: []`` and own no ``tools.yml``.
# Use case: an output-formatting / rendering skill (e.g. one that
# governs an ``echarts-chart`` markdown fence the chat UI parses) --
# the LLM is loaded the body via ``_load_skill`` exactly like any
# other skill but the body just doesn't tell it to call any tool.

_MIN_TOOL_LESS_PROMPT = "# test\n## When to activate\nAlways when the user asks for X.\n"


def _make_minimal_tool_less_skill(skill_dir: Path) -> None:
    """Create a minimal valid tool-less skill (skill.yml + prompt.md only)."""
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "skill.yml").write_text(
        yaml.dump(
            {
                "name": "renderer",
                "description": "Format output as X.",
                "triggers": ["render X"],
                "tools": [],
            }
        )
    )
    (skill_dir / "prompt.md").write_text(_MIN_TOOL_LESS_PROMPT)


class TestToollessSkill:
    """Skills that declare ``tools: []`` (prompt-only routing targets)
    relax two contracts: ``tools.yml`` is not required, and
    ``prompt.md`` has no mandatory headings (activation criteria live
    in ``skill.yml#description`` + ``skill.yml#triggers`` and are
    surfaced through the routing index, so re-stating them in the body
    would be redundant).  These tests pin the relaxations and guard
    against accidental tightening that would break legitimate tool-less
    use cases."""

    def test_tool_less_skill_passes_with_empty_tools_list(
        self,
        tmp_path: Path,
    ) -> None:
        skill_dir = tmp_path / "renderer"
        _make_minimal_tool_less_skill(skill_dir)
        errors = validate_skill_dir(skill_dir)
        error_msgs = [e for e in errors if e.severity == "error"]
        assert error_msgs == [], f"tool-less skill should validate cleanly; got: {[str(e) for e in error_msgs]}"

    def test_tool_less_skill_passes_without_tools_yml(
        self,
        tmp_path: Path,
    ) -> None:
        """Specifically: no ``tools.yml`` file, and the validator does
        NOT complain about it."""
        skill_dir = tmp_path / "renderer"
        _make_minimal_tool_less_skill(skill_dir)
        assert not (skill_dir / "tools.yml").exists()

        errors = validate_skill_dir(skill_dir)
        tools_yml_errors = [e for e in errors if "tools.yml" in e.path and e.severity == "error"]
        assert tools_yml_errors == []

    def test_tool_less_skill_requires_no_prompt_sections(
        self,
        tmp_path: Path,
    ) -> None:
        """Tool-owning skills must document the full call lifecycle
        (workflow / tool call examples / output format / edge cases).
        Tool-less skills require zero headings -- the activation
        criteria come from ``skill.yml`` (description + triggers)
        which the assembler reads into the routing index, so the body
        is purely operational guidance."""
        skill_dir = tmp_path / "renderer"
        _make_minimal_tool_less_skill(skill_dir)

        errors = validate_skill_dir(skill_dir)
        section_errors = [e for e in errors if "missing required section" in e.message]
        assert section_errors == [], (
            f"tool-less skill should not require any sections; got: {[str(e) for e in section_errors]}"
        )

    def test_tool_less_skill_passes_without_when_to_activate(
        self,
        tmp_path: Path,
    ) -> None:
        """Regression guard for the deprecated ``when to activate``
        requirement: a tool-less prompt with NO standardized headings
        (just renderer-specific guidance) must validate cleanly.  The
        routing decision is made entirely from ``skill.yml``."""
        skill_dir = tmp_path / "renderer"
        _make_minimal_tool_less_skill(skill_dir)
        (skill_dir / "prompt.md").write_text("# test\n\nEmit the result as a fenced ``echarts-chart`` block.\n")

        errors = validate_skill_dir(skill_dir)
        section_errors = [e for e in errors if "missing required section" in e.message]
        assert section_errors == [], (
            f"tool-less skill without 'when to activate' should pass; got: {[str(e) for e in section_errors]}"
        )

    def test_tool_less_skill_still_requires_triggers(
        self,
        tmp_path: Path,
    ) -> None:
        """``triggers`` remains required -- the routing index needs at
        least one phrase to match against the user's request."""
        skill_dir = tmp_path / "renderer"
        skill_dir.mkdir()
        (skill_dir / "skill.yml").write_text(
            yaml.dump(
                {
                    "name": "renderer",
                    "description": "Format output as X.",
                    "triggers": [],
                    "tools": [],
                }
            )
        )
        (skill_dir / "prompt.md").write_text(_MIN_TOOL_LESS_PROMPT)

        errors = validate_skill_dir(skill_dir)
        assert any("triggers" in e.message and "non-empty" in e.message for e in errors)

    def test_tool_owning_skill_still_requires_full_section_set(
        self,
        tmp_path: Path,
    ) -> None:
        """Regression guard: relaxing the rules for tool-less skills
        must NOT relax them for tool-owning skills.  A tool-owning
        skill with only a "when to activate" section should still
        fail with four missing-section errors."""
        skill_dir = tmp_path / "owning"
        skill_dir.mkdir()
        (skill_dir / "skill.yml").write_text(
            yaml.dump(
                {
                    "name": "owning",
                    "description": "Owns a tool.",
                    "triggers": ["t"],
                    "tools": ["t"],
                }
            )
        )
        # Minimal prompt -- only the base "when to activate" section.
        (skill_dir / "prompt.md").write_text(_MIN_TOOL_LESS_PROMPT)
        (skill_dir / "tools.yml").write_text(
            "tools:\n  - name: t\n    type: uc_function\n    function_name: cat.sch.t\n    description: test\n"
        )

        errors = validate_skill_dir(skill_dir)
        section_errors = [e for e in errors if "missing required section" in e.message]
        # Four sections still required for tool-owning skills:
        # workflow, tool call examples, output format, edge cases.
        section_names = {e.message.split("'")[1] for e in section_errors}
        assert section_names == {
            "workflow",
            "tool call examples",
            "output format",
            "edge cases",
        }


class TestPromptMdValidation:
    """Verify prompt.md validation rules."""

    def test_missing_prompt_md(self, tmp_path: Path) -> None:
        skill_dir = tmp_path / "no_prompt"
        skill_dir.mkdir()
        (skill_dir / "skill.yml").write_text(
            yaml.dump(
                {
                    "name": "test",
                    "description": "test",
                    "triggers": ["t"],
                    "tools": ["t"],
                }
            )
        )
        (skill_dir / "tools.yml").write_text(_MIN_TOOLS)
        errors = validate_skill_dir(skill_dir)
        assert any("prompt.md" in e.path and "missing" in e.message for e in errors)

    def test_missing_sections(self, tmp_path: Path) -> None:
        skill_dir = tmp_path / "bad_prompt"
        skill_dir.mkdir()
        (skill_dir / "skill.yml").write_text(
            yaml.dump(
                {
                    "name": "test",
                    "description": "test",
                    "triggers": ["t"],
                    "tools": ["t"],
                }
            )
        )
        (skill_dir / "prompt.md").write_text("# Test\n\nJust some text.\n")
        (skill_dir / "tools.yml").write_text(_MIN_TOOLS)
        errors = validate_skill_dir(skill_dir)
        missing_sections = [e for e in errors if "missing required section" in e.message]
        assert len(missing_sections) == len(["workflow", "tool call examples", "output format", "edge cases"])

    def test_long_prompt_warning(self, tmp_path: Path) -> None:
        skill_dir = tmp_path / "long_prompt"
        skill_dir.mkdir()
        (skill_dir / "skill.yml").write_text(
            yaml.dump(
                {
                    "name": "test",
                    "description": "test",
                    "triggers": ["t"],
                    "tools": ["t"],
                }
            )
        )
        long_text = (
            "# Test\n\n"
            "## When to activate\ntest\n\n"
            "## Workflow\n1. step\n\n"
            "## Tool call examples\n```\ncall()\n```\n\n"
            "## Output format\ntest\n\n"
            "## Edge cases\n- test\n\n"
        )
        long_text += "\n".join(f"Line {i}" for i in range(600))
        (skill_dir / "prompt.md").write_text(long_text)
        (skill_dir / "tools.yml").write_text(_MIN_TOOLS)
        errors = validate_skill_dir(skill_dir)
        warnings = [e for e in errors if e.severity == "warning" and "lines" in e.message]
        assert len(warnings) == 1


class TestToolsYmlValidation:
    """Verify tools.yml validation rules."""

    def test_missing_tools_yml(self, tmp_path: Path) -> None:
        skill_dir = tmp_path / "no_tools"
        skill_dir.mkdir()
        (skill_dir / "skill.yml").write_text(
            yaml.dump(
                {
                    "name": "test",
                    "description": "test",
                    "triggers": ["t"],
                    "tools": ["t"],
                }
            )
        )
        (skill_dir / "prompt.md").write_text(_MIN_PROMPT)
        errors = validate_skill_dir(skill_dir)
        assert any("tools.yml" in e.path and "missing" in e.message for e in errors)

    def test_unknown_tool_type_warning(self, tmp_path: Path) -> None:
        skill_dir = tmp_path / "bad_type"
        skill_dir.mkdir()
        (skill_dir / "skill.yml").write_text(
            yaml.dump(
                {
                    "name": "test",
                    "description": "test",
                    "triggers": ["t"],
                    "tools": ["t"],
                }
            )
        )
        (skill_dir / "prompt.md").write_text(_MIN_PROMPT)
        (skill_dir / "tools.yml").write_text("tools:\n  - name: t\n    type: unknown_type\n    description: test\n")
        errors = validate_skill_dir(skill_dir)
        warnings = [e for e in errors if "unknown tool type" in e.message]
        assert len(warnings) == 1

    def test_genie_does_not_require_space_id(self, tmp_path: Path) -> None:
        """``space_id`` is auto-resolved by the assembler from the matching
        ``genie_spaces/<sub>/space.yml`` whose ``tool_name`` (default: dir
        name) equals ``tool.name``.  Skills no longer need to declare it."""
        skill_dir = tmp_path / "genie_no_space"
        skill_dir.mkdir()
        (skill_dir / "skill.yml").write_text(
            yaml.dump(
                {
                    "name": "test",
                    "description": "test",
                    "triggers": ["t"],
                    "tools": ["t"],
                }
            )
        )
        (skill_dir / "prompt.md").write_text(_MIN_PROMPT)
        (skill_dir / "tools.yml").write_text("tools:\n  - name: t\n    type: genie\n    description: test\n")
        errors = validate_skill_dir(skill_dir)
        type_errors = [e for e in errors if "space_id" in e.message]
        assert len(type_errors) == 0

    def test_genie_explicit_space_id_is_still_valid(self, tmp_path: Path) -> None:
        """An explicit ``space_id`` overrides auto-resolution and is allowed."""
        skill_dir = tmp_path / "genie_ok"
        skill_dir.mkdir()
        (skill_dir / "skill.yml").write_text(
            yaml.dump(
                {
                    "name": "test",
                    "description": "test",
                    "triggers": ["t"],
                    "tools": ["t"],
                }
            )
        )
        (skill_dir / "prompt.md").write_text(_MIN_PROMPT)
        (skill_dir / "tools.yml").write_text(
            "tools:\n  - name: t\n    type: genie\n    space_id: abc-123\n    description: test\n"
        )
        errors = validate_skill_dir(skill_dir)
        type_errors = [e for e in errors if "space_id" in e.message]
        assert len(type_errors) == 0

    def test_vector_search_does_not_require_index_name(self, tmp_path: Path) -> None:
        """``index_name`` / ``endpoint_name`` are auto-resolved by the
        assembler from the matching ``vector_indexes/<sub>/index_config.yml``."""
        skill_dir = tmp_path / "vs_no_index"
        skill_dir.mkdir()
        (skill_dir / "skill.yml").write_text(
            yaml.dump(
                {
                    "name": "test",
                    "description": "test",
                    "triggers": ["t"],
                    "tools": ["t"],
                }
            )
        )
        (skill_dir / "prompt.md").write_text(_MIN_PROMPT)
        (skill_dir / "tools.yml").write_text("tools:\n  - name: t\n    type: vector_search\n    description: test\n")
        errors = validate_skill_dir(skill_dir)
        type_errors = [e for e in errors if "index_name" in e.message]
        assert len(type_errors) == 0

    def test_uc_function_requires_function_name(self, tmp_path: Path) -> None:
        skill_dir = tmp_path / "uc_no_fn"
        skill_dir.mkdir()
        (skill_dir / "skill.yml").write_text(
            yaml.dump(
                {
                    "name": "test",
                    "description": "test",
                    "triggers": ["t"],
                    "tools": ["t"],
                }
            )
        )
        (skill_dir / "prompt.md").write_text(_MIN_PROMPT)
        (skill_dir / "tools.yml").write_text("tools:\n  - name: t\n    type: uc_function\n    description: test\n")
        errors = validate_skill_dir(skill_dir)
        type_errors = [e for e in errors if "function_name" in e.message]
        assert len(type_errors) == 1
        assert type_errors[0].severity == "error"

    def test_python_tool_validates_cleanly(self, tmp_path: Path) -> None:
        """A well-formed ``type: python`` tool produces no warnings or errors
        (in particular, no 'unknown tool type' warning)."""
        skill_dir = tmp_path / "py_ok"
        skill_dir.mkdir()
        (skill_dir / "skill.yml").write_text(
            yaml.dump(
                {
                    "name": "test",
                    "description": "test",
                    "triggers": ["t"],
                    "tools": ["t"],
                }
            )
        )
        (skill_dir / "prompt.md").write_text(_MIN_PROMPT)
        (skill_dir / "tools.yml").write_text(
            "tools:\n  - name: t\n    type: python\n    description: test\n    callable_path: pkg.mod.fn\n"
        )
        errors = validate_skill_dir(skill_dir)
        assert [e for e in errors if "unknown tool type" in e.message] == []
        assert [e for e in errors if "callable_path" in e.message] == []

    def test_python_tool_requires_callable_path(self, tmp_path: Path) -> None:
        skill_dir = tmp_path / "py_no_callable"
        skill_dir.mkdir()
        (skill_dir / "skill.yml").write_text(
            yaml.dump(
                {
                    "name": "test",
                    "description": "test",
                    "triggers": ["t"],
                    "tools": ["t"],
                }
            )
        )
        (skill_dir / "prompt.md").write_text(_MIN_PROMPT)
        (skill_dir / "tools.yml").write_text("tools:\n  - name: t\n    type: python\n    description: test\n")
        errors = validate_skill_dir(skill_dir)
        type_errors = [e for e in errors if "callable_path" in e.message]
        assert len(type_errors) == 1
        assert type_errors[0].severity == "error"


class TestToolCrossReferences:
    """Verify cross-reference checks between skill.yml and tools.yml."""

    def test_missing_tool_in_tools_yml(self, tmp_path: Path) -> None:
        skill_dir = tmp_path / "xref"
        skill_dir.mkdir()
        (skill_dir / "skill.yml").write_text(
            yaml.dump(
                {
                    "name": "test",
                    "description": "test",
                    "triggers": ["t"],
                    "tools": ["tool_a", "tool_b"],
                }
            )
        )
        (skill_dir / "prompt.md").write_text(_MIN_PROMPT)
        (skill_dir / "tools.yml").write_text(
            "tools:\n"
            "  - name: tool_a\n"
            "    type: uc_function\n"
            "    function_name: cat.sch.tool_a\n"
            "    description: test\n"
        )
        errors = validate_skill_dir(skill_dir)
        xref_warnings = [e for e in errors if "tool_b" in e.message]
        assert len(xref_warnings) == 1


class TestInstanceValidation:
    """Verify instance-level validation across domains and skills."""

    def test_validates_domain_and_skills(self, legacy_instance_dir: Path) -> None:
        errors = validate_instance_skills(legacy_instance_dir)
        error_msgs = [e for e in errors if e.severity == "error"]
        assert len(error_msgs) == 0

    def test_validates_flat_skills(self, legacy_flat_instance_dir: Path) -> None:
        errors = validate_instance_skills(legacy_flat_instance_dir)
        error_msgs = [e for e in errors if e.severity == "error"]
        assert len(error_msgs) == 0

    def test_skill_md_next_to_legacy_files_warns_they_are_ignored(self, tmp_path: Path) -> None:
        skill_dir = create_skill(tmp_path, "migrated")
        (skill_dir / "skill.yml").write_text("name: migrated\n")
        errors = validate_skill_dir(skill_dir)
        assert [e.path for e in errors if "ignored" in e.message] == ["migrated/skill.yml"]
        assert not [e for e in errors if e.severity == "error"]

    def test_domain_skill_colliding_with_a_flat_skill_is_a_duplicate(self, legacy_instance_dir: Path) -> None:
        create_skill(resolve_payload_subdir(legacy_instance_dir, "skills"), "alpha_skill")
        errors = validate_instance_skills(legacy_instance_dir)
        assert any("duplicate skill_id 'alpha_skill'" in e.message for e in errors)


# ---------------------------------------------------------------------------
# PR 5 (#111): MCP brick reference shape + deprecated-type warnings
# ---------------------------------------------------------------------------


class TestCollectMcpBrickToolNames:
    """``collect_mcp_brick_tool_names`` walks every server.yml and unions
    their ``tools.allow`` lists (the source-of-truth for valid MCP
    references)."""

    def test_no_mcp_servers_dir_returns_empty(self, tmp_path: Path) -> None:
        assert collect_mcp_brick_tool_names(tmp_path) == set()

    def test_collects_union_across_servers(self, tmp_path: Path) -> None:
        sub_a = resolve_payload_subdir(tmp_path, "mcp_servers") / "github"
        sub_b = resolve_payload_subdir(tmp_path, "mcp_servers") / "uc"
        sub_a.mkdir(parents=True)
        sub_b.mkdir(parents=True)
        (sub_a / "server.yml").write_text(
            "type: external\nconnection_name: x\ntools:\n  allow: [search_issues, list_repos]\n"
        )
        (sub_b / "server.yml").write_text(
            "type: managed_functions\ncatalog: c\nschema: s\ntools:\n  allow: [lookup_status]\n"
        )

        assert collect_mcp_brick_tool_names(tmp_path) == {
            "search_issues",
            "list_repos",
            "lookup_status",
        }

    def test_skips_underscore_prefixed_dirs(self, tmp_path: Path) -> None:
        """``_state``, ``_scaffold`` and similar must not be parsed."""
        sub = resolve_payload_subdir(tmp_path, "mcp_servers") / "_state"
        sub.mkdir(parents=True)
        (sub / "server.yml").write_text("type: external\ntools:\n  allow: [phantom]\n")
        assert collect_mcp_brick_tool_names(tmp_path) == set()

    def test_malformed_server_yml_is_silently_skipped(
        self,
        tmp_path: Path,
    ) -> None:
        """A broken server.yml is the MCP brick validator's job to flag,
        not the skills validator's; we just don't crash."""
        sub = resolve_payload_subdir(tmp_path, "mcp_servers") / "broken"
        sub.mkdir(parents=True)
        (sub / "server.yml").write_text(": not yaml\n  - [\n")
        assert collect_mcp_brick_tool_names(tmp_path) == set()

    def test_dict_form_uses_expose_as_as_alias(self, tmp_path: Path) -> None:
        # Skill tools.yml references the LLM-visible bare name -- which
        # for an aliased entry is ``expose_as``, NOT ``match`` (the
        # latter is the upstream pattern, never seen by the LLM).
        sub = resolve_payload_subdir(tmp_path, "mcp_servers") / "genie"
        sub.mkdir(parents=True)
        (sub / "server.yml").write_text(
            "type: managed_genie\nspace_id: sp_x\n"
            "tools:\n"
            "  allow:\n"
            '    - match: "query_space_*"\n'
            "      expose_as: query_space\n"
            '    - match: "poll_response_*"\n'
            "      expose_as: poll_response\n"
        )
        assert collect_mcp_brick_tool_names(tmp_path) == {
            "query_space",
            "poll_response",
        }

    def test_dict_form_match_only_uses_literal_match(
        self,
        tmp_path: Path,
    ) -> None:
        # No expose_as, but match is a literal string -- alias = match.
        # Equivalent to the bare-string shape, just spelled out.
        sub = resolve_payload_subdir(tmp_path, "mcp_servers") / "x"
        sub.mkdir(parents=True)
        (sub / "server.yml").write_text("type: managed_sql\ntools:\n  allow:\n    - match: execute_sql_read_only\n")
        assert collect_mcp_brick_tool_names(tmp_path) == {
            "execute_sql_read_only",
        }

    def test_glob_match_without_alias_is_skipped(
        self,
        tmp_path: Path,
    ) -> None:
        # A glob without expose_as produces variable upstream-derived
        # names that the validator cannot enumerate up front.  Skipping
        # is the safe default -- the deployer must add expose_as if a
        # skill needs to reference one of these tools.
        sub = resolve_payload_subdir(tmp_path, "mcp_servers") / "x"
        sub.mkdir(parents=True)
        (sub / "server.yml").write_text('type: managed_sql\ntools:\n  allow:\n    - match: "poll_*"\n')
        assert collect_mcp_brick_tool_names(tmp_path) == set()

    def test_mixed_str_and_dict_entries(self, tmp_path: Path) -> None:
        # A single allow list can mix bare strings and dict entries;
        # all aliases land in the union.
        sub = resolve_payload_subdir(tmp_path, "mcp_servers") / "x"
        sub.mkdir(parents=True)
        (sub / "server.yml").write_text(
            "type: managed_sql\n"
            "tools:\n"
            "  allow:\n"
            "    - execute_sql_read_only\n"
            '    - match: "poll_*"\n'
            "      expose_as: poll\n"
        )
        assert collect_mcp_brick_tool_names(tmp_path) == {
            "execute_sql_read_only",
            "poll",
        }


class TestMcpReferenceResolution:
    """``type: mcp`` (and untyped) entries are cross-checked against the
    union of every ``bricks/mcp/`` server's ``tools.allow``."""

    def test_resolved_mcp_reference_passes(self, tmp_path: Path) -> None:
        """An entry whose name matches a brick allow-list entry is valid."""
        _make_minimal_skill(
            resolve_payload_subdir(tmp_path, "skills") / "s1",
            "tools:\n  - name: search_issues\n    type: mcp\n    description: ok\n",
        )
        sub = resolve_payload_subdir(tmp_path, "mcp_servers") / "github"
        sub.mkdir(parents=True)
        (sub / "server.yml").write_text("type: external\ntools:\n  allow: [search_issues]\n")

        errors = validate_instance_skills(tmp_path)
        ref_errors = [e for e in errors if e.severity == "error" and "MCP tool reference" in e.message]
        assert ref_errors == []

    def test_untyped_entry_is_treated_as_mcp_reference(
        self,
        tmp_path: Path,
    ) -> None:
        """The new ``{name, description}`` shape resolves the same way as
        ``type: mcp``."""
        _make_minimal_skill(
            resolve_payload_subdir(tmp_path, "skills") / "s1",
            "tools:\n  - name: search_issues\n    description: ok\n",
        )
        sub = resolve_payload_subdir(tmp_path, "mcp_servers") / "github"
        sub.mkdir(parents=True)
        (sub / "server.yml").write_text("type: external\ntools:\n  allow: [search_issues]\n")

        errors = validate_instance_skills(tmp_path)
        ref_errors = [e for e in errors if e.severity == "error" and "MCP tool reference" in e.message]
        assert ref_errors == []

    def test_unresolved_mcp_reference_is_an_error(self, tmp_path: Path) -> None:
        _make_minimal_skill(
            resolve_payload_subdir(tmp_path, "skills") / "s1",
            "tools:\n  - name: not_registered\n    type: mcp\n    description: x\n",
        )
        sub = resolve_payload_subdir(tmp_path, "mcp_servers") / "github"
        sub.mkdir(parents=True)
        (sub / "server.yml").write_text("type: external\ntools:\n  allow: [search_issues]\n")

        errors = validate_instance_skills(tmp_path)
        ref_errors = [e for e in errors if e.severity == "error" and "MCP tool reference" in e.message]
        assert len(ref_errors) == 1
        assert "not_registered" in ref_errors[0].message
        assert "Did you forget to register" in ref_errors[0].message

    def test_mcp_reference_skipped_without_instance_context(
        self,
        tmp_path: Path,
    ) -> None:
        """``--skill-dir`` mode (no instance) skips the cross-check."""
        skill_dir = tmp_path / "s1"
        _make_minimal_skill(
            skill_dir,
            "tools:\n  - name: anything\n    type: mcp\n    description: x\n",
        )
        errors = validate_skill_dir(skill_dir)  # mcp_brick_tools=None
        assert not any("MCP tool reference" in e.message for e in errors)


class TestRemovedMcpTypes:
    """``managed_mcp`` / ``external_mcp`` are no longer tool types."""

    @pytest.mark.parametrize("removed_type", ["managed_mcp", "external_mcp"])
    def test_reported_as_unknown_type(self, tmp_path: Path, removed_type: str) -> None:
        _make_minimal_skill(
            tmp_path / "s1",
            f"tools:\n  - name: x\n    type: {removed_type}\n    url: https://h\n    description: legacy\n",
        )
        errors = validate_skill_dir(tmp_path / "s1")
        assert any(f"unknown tool type '{removed_type}'" in e.message for e in errors)


# ---------------------------------------------------------------------------
# Assembly: legacy skills are flattened into the shared SKILL.md rendering
# ---------------------------------------------------------------------------


class TestLegacyAssembly:
    """Domains are flattened; triggers, phase and ``domain_prompt.md`` no longer reach the prompt."""

    def test_reports_the_domain_it_flattened(self, legacy_assembled: dict[str, Any]) -> None:
        assert legacy_assembled["domain_names"] == ["test_domain"]

    def test_skills_are_sorted_by_skill_id_not_phase(self, legacy_assembled: dict[str, Any]) -> None:
        assert legacy_assembled["skill_names"] == ["alpha_skill", "beta_skill"]

    def test_renders_like_skill_md_skills(self, legacy_assembled: dict[str, Any]) -> None:
        prompt = legacy_assembled["system_prompt"]
        assert "## Skills" in prompt
        assert "### Skill: alpha_skill" in prompt
        assert "Test skill: alpha_skill" in prompt
        assert "Call test_tool" in prompt

    def test_drops_domain_sections_and_triggers(self, legacy_assembled: dict[str, Any]) -> None:
        prompt = legacy_assembled["system_prompt"]
        assert "Skill Domain" not in prompt
        assert "Test entities and relationships." not in prompt
        assert "trigger for alpha_skill" not in prompt

    def test_flat_legacy_skills_are_discovered(self, legacy_flat_instance_dir: Path) -> None:
        result = assemble_skills(legacy_flat_instance_dir)
        assert result["skill_names"] == ["alpha_skill", "beta_skill"]
        assert result["domain_names"] == []

    def test_lazy_index_uses_directory_names_and_prompt_md(self, legacy_instance_dir: Path) -> None:
        set_lazy_loading(legacy_instance_dir)
        result = assemble_skills(legacy_instance_dir)
        assert result["skill_index"] == {
            "alpha_skill": "test_domain/alpha_skill/prompt.md",
            "beta_skill": "test_domain/beta_skill/prompt.md",
        }

    def test_mixes_with_skill_md_skills(self, legacy_instance_dir: Path) -> None:
        create_skill(resolve_payload_subdir(legacy_instance_dir, "skills"), "gamma-skill")
        result = assemble_skills(legacy_instance_dir)
        assert result["skill_names"] == ["alpha_skill", "beta_skill", "gamma-skill"]


class TestSkillMetadataValidation:
    """The assembler validates skill.yml against SkillMetadata at the read
    boundary so authors get a clear, path-prefixed error instead of a
    later AttributeError or wrong-type explosion."""

    def test_invalid_phase_type_raises_with_path(self, tmp_path: Path) -> None:
        skill_dir = tmp_path / "bad_skill"
        skill_dir.mkdir()
        (skill_dir / "skill.yml").write_text(
            "name: bad_skill\ndescription: x\nphase: not_a_number\n",
        )

        with pytest.raises(ValueError, match=r"bad_skill[\\/]skill\.yml.*invalid skill metadata"):
            load_skill(skill_dir)

    def test_missing_name_returns_none(self, tmp_path: Path) -> None:
        """Historical behaviour: missing-name silently skips."""
        skill_dir = tmp_path / "no_name"
        skill_dir.mkdir()
        (skill_dir / "skill.yml").write_text("description: x\n")

        assert load_skill(skill_dir) is None


class TestToolManifest:
    """Verify the merged ``SKILL_TOOLS`` manifest from ``tools.yml`` files."""

    def test_deduplicates_tools(self, legacy_assembled: dict[str, Any]) -> None:
        names = [t["name"] for t in legacy_assembled["tools"]]
        assert len(names) == len(set(names))

    def test_all_tools_present(self, legacy_assembled: dict[str, Any]) -> None:
        names = {t["name"] for t in legacy_assembled["tools"]}
        assert names == {"tool_a", "tool_b", "tool_c"}

    def test_shared_tool_tracks_users(self, legacy_assembled: dict[str, Any]) -> None:
        tool_b = next(t for t in legacy_assembled["tools"] if t["name"] == "tool_b")
        assert "alpha_skill" in tool_b["used_by"]
        assert "beta_skill" in tool_b["used_by"]

    def test_invalid_tool_type_surfaces_file_path(self, legacy_instance_dir: Path) -> None:
        skill_dir = resolve_payload_subdir(legacy_instance_dir, "skills") / "test_domain" / "alpha_skill"
        tools_yml = skill_dir / "tools.yml"
        tools_yml.write_text(
            yaml.safe_dump(
                {"tools": [{"name": "bogus_tool", "type": "definitely_not_a_real_type", "description": "oops"}]}
            ),
            encoding="utf-8",
        )

        with pytest.raises(ValueError) as excinfo:
            assemble_skills(legacy_instance_dir)
        assert str(tools_yml) in str(excinfo.value)


# ---------------------------------------------------------------------------
# Auto-wiring of genie / vector_search backends to skill tools
# ---------------------------------------------------------------------------


def _make_genie_sub(instance_dir: Path, sub: str, tool_name: str | None = None) -> None:
    """Create a minimal ``<inst>/genie_spaces/<sub>/space.yml``."""
    sub_dir = instance_dir / "genie_spaces" / sub
    sub_dir.mkdir(parents=True, exist_ok=True)
    body = "description: test genie\n"
    if tool_name is not None:
        body += f"tool_name: {tool_name}\n"
    (sub_dir / "space.yml").write_text(body, encoding="utf-8")


def _make_vs_sub(instance_dir: Path, sub: str, tool_name: str | None = None) -> None:
    sub_dir = instance_dir / "vector_indexes" / sub
    sub_dir.mkdir(parents=True, exist_ok=True)
    body = "description: test vs\n"
    if tool_name is not None:
        body += f"tool_name: {tool_name}\n"
    (sub_dir / "index_config.yml").write_text(body, encoding="utf-8")


class TestResolveSkillToolRefs:
    """Auto-wiring of genie / vector_search tools to their backing
    sub-instances. Skills declare ``name`` + ``type``; the assembler
    injects ``space_id``/``index_name`` from variables based on
    ``tool_name`` declared in space.yml/index_config.yml (or the dir
    name as fallback)."""

    def test_genie_resolves_via_dir_name_fallback(self, tmp_path: Path) -> None:
        _make_genie_sub(tmp_path, "info_schema")
        tools: list[Tool] = [GenieTool(name="info_schema", description="x")]
        _resolve_skill_tool_refs(tools, tmp_path, {"genie_space_id__info_schema": "sp-abc"})
        assert isinstance(tools[0], GenieTool)
        assert tools[0].space_id == "sp-abc"

    def test_genie_resolves_via_explicit_tool_name(self, tmp_path: Path) -> None:
        _make_genie_sub(tmp_path, "info_schema", tool_name="catalog_genie")
        tools: list[Tool] = [GenieTool(name="catalog_genie", description="x")]
        _resolve_skill_tool_refs(tools, tmp_path, {"genie_space_id__info_schema": "sp-abc"})
        assert isinstance(tools[0], GenieTool)
        assert tools[0].space_id == "sp-abc"

    def test_genie_explicit_space_id_wins_over_resolver(self, tmp_path: Path) -> None:
        _make_genie_sub(tmp_path, "info_schema", tool_name="catalog_genie")
        tools: list[Tool] = [GenieTool(name="catalog_genie", description="x", space_id="explicit-sp")]
        _resolve_skill_tool_refs(tools, tmp_path, {"genie_space_id__info_schema": "sp-abc"})
        assert isinstance(tools[0], GenieTool)
        assert tools[0].space_id == "explicit-sp"

    def test_genie_no_match_leaves_field_empty(self, tmp_path: Path) -> None:
        _make_genie_sub(tmp_path, "info_schema")
        tools: list[Tool] = [GenieTool(name="unmatched_tool", description="x")]
        _resolve_skill_tool_refs(tools, tmp_path, {})
        assert isinstance(tools[0], GenieTool)
        assert tools[0].space_id is None

    def test_genie_duplicate_tool_name_raises(self, tmp_path: Path) -> None:
        _make_genie_sub(tmp_path, "info_schema", tool_name="catalog_genie")
        _make_genie_sub(tmp_path, "other_dir", tool_name="catalog_genie")
        tools: list[Tool] = [GenieTool(name="catalog_genie", description="x")]
        with pytest.raises(ValueError, match="Duplicate tool_name"):
            _resolve_skill_tool_refs(tools, tmp_path, {})

    def test_vs_resolves_index_and_endpoint(self, tmp_path: Path) -> None:
        _make_vs_sub(tmp_path, "docs_search")
        tools: list[Tool] = [VectorSearchTool(name="docs_search", description="x")]
        _resolve_skill_tool_refs(
            tools,
            tmp_path,
            {"vs_index_name__docs_search": "main.idx.docs", "vs_endpoint_name__docs_search": "vs-ep"},
        )
        assert isinstance(tools[0], VectorSearchTool)
        assert tools[0].index_name == "main.idx.docs"
        assert tools[0].endpoint_name == "vs-ep"

    def test_assemble_skills_runs_resolver_end_to_end(self, legacy_instance_dir: Path) -> None:
        """A skill declares only name+type+description and gets a resolved space_id via variables."""
        skill_dir = resolve_payload_subdir(legacy_instance_dir, "skills") / "test_domain" / "alpha_skill"
        (skill_dir / "tools.yml").write_text(
            yaml.safe_dump(
                {"tools": [{"name": "catalog_genie", "type": "genie", "description": "Query catalog metadata."}]}
            ),
            encoding="utf-8",
        )

        _make_genie_sub(legacy_instance_dir, "info_schema", tool_name="catalog_genie")
        result = assemble_skills(legacy_instance_dir, {"genie_space_id__info_schema": "sp-resolved"})
        catalog_tool = next(t for t in result["tools"] if t["name"] == "catalog_genie")
        assert catalog_tool["space_id"] == "sp-resolved"


# ---------------------------------------------------------------------------
# PR 5 (#111): MCPTool normalisation + filtering from SKILL_TOOLS dump
# ---------------------------------------------------------------------------


class TestMcpShapeNormalisation:
    """The ``{name, description}`` shape is normalised to ``type: "mcp"`` before contract validation."""

    def test_untyped_entry_normalises_to_mcp(self) -> None:
        norm = _normalize_tool_manifest({"tools": [{"name": "x", "description": "d"}]})
        assert norm["tools"] == [{"name": "x", "description": "d", "type": "mcp"}]

    def test_typed_entries_left_unmodified(self) -> None:
        raw = {
            "tools": [
                {"name": "g", "type": "genie", "description": "d"},
                {"name": "v", "type": "vector_search", "description": "d"},
            ]
        }
        assert _normalize_tool_manifest(raw)["tools"] == raw["tools"]

    def test_normalisation_does_not_mutate_input(self) -> None:
        raw = {"tools": [{"name": "x", "description": "d"}]}
        original_entry = dict(raw["tools"][0])
        _normalize_tool_manifest(raw)
        assert raw["tools"][0] == original_entry


def _write_tools_skill(tmp_path: Path, tool_names: list[str], tools: list[dict[str, Any]]) -> None:
    """A flat legacy skill ``s1`` owning *tools*."""
    _make_minimal_skill(resolve_payload_subdir(tmp_path, "skills") / "s1", yaml.safe_dump({"tools": tools}))
    skill_yml = resolve_payload_subdir(tmp_path, "skills") / "s1" / "skill.yml"
    skill_yml.write_text(
        yaml.safe_dump({"name": "s1", "description": "d", "triggers": ["t"], "tools": tool_names}),
    )


class TestSkillToolsDumpFiltersMcpVariants:
    """MCPTool references never reach ``SKILL_TOOLS``; direct-mode tools do (PR 5)."""

    def test_mcp_reference_dropped(self, tmp_path: Path) -> None:
        _write_tools_skill(
            tmp_path,
            ["mcp_ref", "uc"],
            [
                {"name": "mcp_ref", "description": "Brick-served"},
                {"name": "uc", "type": "uc_function", "function_name": "c.s.f", "description": "UC"},
            ],
        )
        assert [t["name"] for t in assemble_skills(tmp_path)["tools"]] == ["uc"]

    def test_removed_managed_mcp_type_is_rejected(self, tmp_path: Path) -> None:
        _write_tools_skill(
            tmp_path,
            ["legacy"],
            [{"name": "legacy", "type": "managed_mcp", "url": "https://h", "description": "old"}],
        )
        with pytest.raises(ValueError, match="invalid tool manifest"):
            assemble_skills(tmp_path)

    def test_python_tool_survives_and_round_trips(self, tmp_path: Path) -> None:
        schema = {"type": "object", "properties": {"name": {"type": "string"}}}
        _write_tools_skill(
            tmp_path,
            ["add_signal"],
            [
                {
                    "name": "add_signal",
                    "type": "python",
                    "description": "Add a signal.",
                    "callable_path": "my_pkg.mod.add_signal",
                    "params_json_schema": schema,
                }
            ],
        )
        result = assemble_skills(tmp_path)
        assert len(result["tools"]) == 1
        entry = result["tools"][0]
        assert entry["type"] == "python"
        assert entry["callable_path"] == "my_pkg.mod.add_signal"
        assert entry["params_json_schema"] == schema
