"""Tests for the skills assembler (no workspace required)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_foundry._instance_layout import resolve_payload_subdir
from payloads.agent.contracts import DEFAULT_SKILL_ROUTING_HEADER
from payloads.agent.deploy.skills.assemble import (
    assemble_skills,
    discover_skills,
    load_skill,
    resolve_skill_loading_mode,
)
from payloads.agent.tests.unit.deploy.skills.conftest import author_skill_spec, set_lazy_loading


class TestDiscovery:
    """Verify ``skills/<skill_id>/SKILL.md`` discovery."""

    def test_discovers_skills_sorted_by_skill_id(self, assembled: dict[str, Any]) -> None:
        assert assembled["skill_names"] == ["alpha-skill", "beta-skill"]
        assert assembled["domain_names"] == []

    def test_skill_id_is_the_directory_name(self, instance_dir: Path) -> None:
        skills, _ = discover_skills(instance_dir)
        assert [s.skill_id for s in skills] == ["alpha-skill", "beta-skill"]

    def test_ignores_underscore_dirs(self, instance_dir: Path) -> None:
        shared_dir = resolve_payload_subdir(instance_dir, "skills") / "_shared"
        shared_dir.mkdir(parents=True)
        (shared_dir / "SKILL.md").write_text("---\nname: _shared\ndescription: x\n---\n\nbody\n")

        skills, _ = discover_skills(instance_dir)
        assert "_shared" not in [s.skill_id for s in skills]

    def test_ignores_dirs_without_skill_md(self, instance_dir: Path) -> None:
        (resolve_payload_subdir(instance_dir, "skills") / "notes").mkdir()
        skills, _ = discover_skills(instance_dir)
        assert len(skills) == 2

    def test_empty_skills_dir(self, tmp_path: Path) -> None:
        inst = tmp_path / "empty_inst"
        resolve_payload_subdir(inst, "skills").mkdir(parents=True)

        result = assemble_skills(inst)
        assert result["skill_names"] == []
        assert result["domain_names"] == []
        assert result["tools"] == []

    def test_no_skills_dir(self, tmp_path: Path) -> None:
        inst = tmp_path / "no_skills"
        inst.mkdir()

        result = assemble_skills(inst)
        assert result["skill_names"] == []
        assert result["domain_names"] == []

    def test_invalid_skill_md_raises_with_path(self, tmp_path: Path) -> None:
        skill_dir = tmp_path / "bad-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("# no frontmatter\n")

        with pytest.raises(ValueError, match=r"bad-skill[\\/]SKILL\.md"):
            load_skill(skill_dir)


class TestPromptComposition:
    """Eager mode: the agent prompt, then every skill's name, description and body."""

    def test_contains_agent_prompt(self, assembled: dict[str, Any]) -> None:
        assert "You are a test assistant." in assembled["system_prompt"]

    def test_the_agent_prompt_leads_into_the_skills(self, assembled: dict[str, Any]) -> None:
        prompt = assembled["system_prompt"]
        assert prompt.index("You are a test assistant.") < prompt.index("## Skills")

    def test_contains_skill_sections_in_skill_id_order(self, assembled: dict[str, Any]) -> None:
        prompt = assembled["system_prompt"]
        assert prompt.index("### Skill: alpha-skill") < prompt.index("### Skill: beta-skill")

    def test_contains_description_and_body(self, assembled: dict[str, Any]) -> None:
        prompt = assembled["system_prompt"]
        assert "Test skill: beta-skill" in prompt
        assert "Call test_tool" in prompt

    def test_frontmatter_is_not_rendered(self, assembled: dict[str, Any]) -> None:
        assert "name: alpha-skill" not in assembled["system_prompt"]

    def test_carries_no_routing_index(self, assembled: dict[str, Any]) -> None:
        """Eager bodies make an index redundant."""
        assert "## Loadable skills" not in assembled["system_prompt"]

    def test_skill_md_skills_contribute_no_tools(self, assembled: dict[str, Any]) -> None:
        assert assembled["tools"] == []


class TestEvalAggregation:
    """Verify eval question aggregation."""

    def test_questions_from_all_skills(self, assembled: dict[str, Any]) -> None:
        assert len(assembled["eval_questions"]) == 6

    def test_questions_tagged_with_skill(self, assembled: dict[str, Any]) -> None:
        skill_tags = {q["skill"] for q in assembled["eval_questions"]}
        assert skill_tags == {"alpha-skill", "beta-skill"}


class TestAssemblyIdempotent:
    """Verify assembly produces consistent results."""

    def test_same_input_same_output(self, instance_dir: Path) -> None:
        assert assemble_skills(instance_dir) == assemble_skills(instance_dir)


# ---------------------------------------------------------------------------
# Lazy skill loading -- issue #98 PR 1/2
# ---------------------------------------------------------------------------


class TestPromptCacheOverridesLazy:
    """Caching and lazy loading are mutually exclusive, and caching wins.

    Lazy skill bodies arrive as tool results behind the volatile tail of
    the conversation, so they are re-sent and re-written at full price on
    every request instead of riding the cached prefix -- measured at 3x the
    cost of eager on a multi-skill question.  Allowing the pair to coexist
    silently would make the framework's default the expensive combination.
    """

    def test_lazy_becomes_eager_when_caching_is_on(self, instance_dir: Path) -> None:
        author_skill_spec(instance_dir, loading="lazy", prompt_cache_enabled=True)

        result = assemble_skills(instance_dir)

        assert result["skill_loading"] == "eager"
        assert result["skill_index"] is None, "an index would re-enable _load_skill at runtime"
        assert "### Skill: alpha-skill" in result["system_prompt"], "bodies belong in the cached prefix"

    def test_the_override_is_announced(self, instance_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Silently changing an authored value is how deploys become mysteries."""
        author_skill_spec(instance_dir, loading="lazy", prompt_cache_enabled=True)
        assemble_skills(instance_dir)

        captured = capsys.readouterr().out
        assert "lazy" in captured
        assert "prompt_cache" in captured

    def test_lazy_survives_when_caching_is_off(self, instance_dir: Path) -> None:
        set_lazy_loading(instance_dir)
        assert assemble_skills(instance_dir)["skill_loading"] == "lazy"

    @pytest.mark.parametrize(
        ("authored", "cache_on", "expected"),
        [
            (None, True, "eager"),
            (None, False, "eager"),
            ("eager", True, "eager"),
            ("eager", False, "eager"),
            ("lazy", True, "eager"),
            ("lazy", False, "lazy"),
        ],
    )
    def test_resolution_table(self, authored: str | None, cache_on: bool, expected: str) -> None:
        resolved = resolve_skill_loading_mode(authored, prompt_cache_enabled=cache_on)  # type: ignore[arg-type]

        assert resolved == expected


class TestSkillLoadingMode:
    """Eager mode (``loading`` absent or ``eager``) inlines the bodies and
    returns no ``skill_index``.  Lazy mode emits the same ``## Loadable
    skills`` index that user and gateway skills use, plus a
    ``skill_id -> body_path`` map for ``_load_skill``.
    """

    def test_eager_mode_is_default_when_field_absent(self, assembled: dict[str, Any]) -> None:
        assert assembled["skill_loading"] == "eager"
        assert assembled["skill_index"] is None

    def test_lazy_mode_emits_index_only(self, instance_dir: Path) -> None:
        set_lazy_loading(instance_dir)
        prompt = assemble_skills(instance_dir)["system_prompt"]
        assert "## Loadable skills" in prompt
        assert "### Skill: alpha-skill" not in prompt
        assert "Call test_tool" not in prompt

    def test_lazy_mode_index_rows_carry_id_name_and_description(self, instance_dir: Path) -> None:
        set_lazy_loading(instance_dir)
        prompt = assemble_skills(instance_dir)["system_prompt"]
        assert "- `alpha-skill` -- alpha-skill: Test skill: alpha-skill" in prompt
        assert "- `beta-skill` -- beta-skill: Test skill: beta-skill" in prompt

    def test_lazy_mode_skill_index_maps_ids_to_relative_skill_md(self, instance_dir: Path) -> None:
        set_lazy_loading(instance_dir)
        assert assemble_skills(instance_dir)["skill_index"] == {
            "alpha-skill": "alpha-skill/SKILL.md",
            "beta-skill": "beta-skill/SKILL.md",
        }

    def test_lazy_mode_preserves_names_and_tools(self, instance_dir: Path) -> None:
        eager_result = assemble_skills(instance_dir)
        set_lazy_loading(instance_dir)
        lazy_result = assemble_skills(instance_dir)
        assert lazy_result["tools"] == eager_result["tools"]
        assert lazy_result["skill_names"] == eager_result["skill_names"]


class TestSkillRoutingHeader:
    """Configurable lazy-mode index prose (``routing_header``).

    The header defaults to the shared framework constant and can be
    overridden per instance.  The ``## Loadable skills`` heading and the
    rows stay fixed regardless.
    """

    def test_default_header_used_when_field_absent(self, instance_dir: Path) -> None:
        set_lazy_loading(instance_dir)
        assert DEFAULT_SKILL_ROUTING_HEADER in assemble_skills(instance_dir)["system_prompt"]

    def test_custom_header_appears_verbatim(self, instance_dir: Path) -> None:
        custom = "Load as many skills as the request needs, then act."
        set_lazy_loading(instance_dir)
        author_skill_spec(instance_dir, routing_header=custom)
        prompt = assemble_skills(instance_dir)["system_prompt"]
        assert custom in prompt
        assert DEFAULT_SKILL_ROUTING_HEADER not in prompt
        assert "## Loadable skills" in prompt
        assert "- `alpha-skill` --" in prompt

    def test_header_ignored_in_eager_mode(self, instance_dir: Path) -> None:
        """Eager mode has no index, so the header prose has nowhere to leak to."""
        custom = "THIS SHOULD ONLY APPEAR IN LAZY MODE"
        author_skill_spec(instance_dir, routing_header=custom)
        result = assemble_skills(instance_dir)
        assert result["skill_loading"] == "eager"
        assert custom not in result["system_prompt"]
