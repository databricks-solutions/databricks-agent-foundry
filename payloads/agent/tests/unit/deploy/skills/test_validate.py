"""Tests for the skills validator (no workspace required)."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_foundry._instance_layout import resolve_payload_subdir
from agent_foundry.contracts.skills import MAX_SKILL_DESCRIPTION_CHARS, render_skill_md
from payloads.agent.deploy.skills.common import ValidationError
from payloads.agent.deploy.skills.validate import (
    _emit_duplicate_skill_id_errors,
    _gather_skill_id_paths,
    validate_instance_skills,
    validate_skill_dir,
)
from payloads.agent.tests.unit.deploy.skills.conftest import author_skill_spec, create_skill


def _errors(errors: list[ValidationError]) -> list[str]:
    return [str(e) for e in errors if e.severity == "error"]


def _write_skill_md(skill_dir: Path, text: str) -> Path:
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(text, encoding="utf-8")
    return skill_dir


class TestSkillMdValidation:
    """``skills/<skill_id>/SKILL.md`` rules."""

    def test_valid_skill_passes_without_findings(self, tmp_path: Path) -> None:
        assert validate_skill_dir(create_skill(tmp_path, "sql-runner")) == []

    def test_missing_skill_md(self, tmp_path: Path) -> None:
        skill_dir = tmp_path / "empty-skill"
        skill_dir.mkdir()
        errors = validate_skill_dir(skill_dir)
        assert [(e.path, e.message) for e in errors] == [("empty-skill/SKILL.md", "file is missing")]

    @pytest.mark.parametrize("dir_name", ["sql_runner", "SqlRunner", "-sql", "sql--runner"])
    def test_directory_name_must_be_a_slug(self, tmp_path: Path, dir_name: str) -> None:
        skill_dir = _write_skill_md(tmp_path / dir_name, render_skill_md(dir_name, "d", "body"))
        assert any("directory name must be lowercase" in e for e in _errors(validate_skill_dir(skill_dir)))

    def test_name_must_equal_directory_name(self, tmp_path: Path) -> None:
        skill_dir = _write_skill_md(tmp_path / "sql-runner", render_skill_md("other", "d", "body"))
        assert any("must equal the directory name" in e for e in _errors(validate_skill_dir(skill_dir)))

    def test_missing_frontmatter_is_an_error(self, tmp_path: Path) -> None:
        skill_dir = _write_skill_md(tmp_path / "sql-runner", "# Just a body\n")
        assert any("SKILL.md" in e for e in _errors(validate_skill_dir(skill_dir)))

    def test_description_over_the_cap_is_an_error(self, tmp_path: Path) -> None:
        too_long = "x" * (MAX_SKILL_DESCRIPTION_CHARS + 1)
        skill_dir = _write_skill_md(tmp_path / "sql-runner", render_skill_md("sql-runner", too_long, "body"))
        assert any("description" in e for e in _errors(validate_skill_dir(skill_dir)))

    def test_empty_body_is_an_error(self, tmp_path: Path) -> None:
        skill_dir = _write_skill_md(tmp_path / "sql-runner", render_skill_md("sql-runner", "d", ""))
        assert any("body is empty" in e for e in _errors(validate_skill_dir(skill_dir)))

    def test_long_file_warns(self, tmp_path: Path) -> None:
        body = "\n".join(f"Line {i}" for i in range(600))
        skill_dir = create_skill(tmp_path, "long-skill", body=body)
        warnings = [e for e in validate_skill_dir(skill_dir) if e.severity == "warning" and "lines" in e.message]
        assert len(warnings) == 1


class TestEvalValidation:
    """Verify eval/questions.yml validation rules."""

    def test_missing_eval_warning(self, tmp_path: Path) -> None:
        skill_dir = create_skill(tmp_path, "no-eval")
        (skill_dir / "eval" / "questions.yml").unlink()
        warnings = [e for e in validate_skill_dir(skill_dir) if "eval" in e.path and e.severity == "warning"]
        assert len(warnings) == 1

    def test_few_questions_warning(self, tmp_path: Path) -> None:
        skill_dir = create_skill(tmp_path, "few-eval", eval_questions=1)
        warnings = [e for e in validate_skill_dir(skill_dir) if "question" in e.message and e.severity == "warning"]
        assert len(warnings) == 1


class TestInstanceValidation:
    """Verify instance-level validation across skills."""

    def test_valid_instance_passes(self, instance_dir: Path) -> None:
        assert _errors(validate_instance_skills(instance_dir)) == []

    def test_empty_skills_dir_warning(self, tmp_path: Path) -> None:
        resolve_payload_subdir(tmp_path, "skills").mkdir(parents=True)
        errors = validate_instance_skills(tmp_path)
        assert any("no skill directories with a SKILL.md found" in e.message for e in errors)

    def test_underscore_directories_are_ignored(self, instance_dir: Path) -> None:
        (resolve_payload_subdir(instance_dir, "skills") / "_shared").mkdir()
        assert validate_instance_skills(instance_dir) == []


# ---------------------------------------------------------------------------
# Lazy skill loading -- issue #98 PR 1/2
# ---------------------------------------------------------------------------


class TestSkillLoadingMode:
    """Issue #98 PR 1/2: validator gates on lazy-mode invariants."""

    def test_eager_value_passes(self, instance_dir: Path) -> None:
        author_skill_spec(instance_dir, loading="eager")
        errors = validate_instance_skills(instance_dir)
        assert not any("skills.loading" in e.message for e in errors)

    def test_lazy_with_caching_on_warns_that_it_will_be_ignored(self, instance_dir: Path) -> None:
        """The deploy switches to eager; the author should hear it here first."""
        author_skill_spec(instance_dir, loading="lazy")

        errors = validate_instance_skills(instance_dir)

        assert not any("skills.loading" in e for e in _errors(errors))
        warnings = [e for e in errors if "skills.loading" in e.message and e.severity == "warning"]
        assert len(warnings) == 1
        assert "prompt_cache" in warnings[0].message

    def test_lazy_with_caching_off_is_silent(self, instance_dir: Path) -> None:
        author_skill_spec(instance_dir, loading="lazy", prompt_cache_enabled=False)
        errors = validate_instance_skills(instance_dir)
        assert not any("skills.loading" in e.message for e in errors)

    def test_invalid_skill_loading_value_is_error(self, instance_dir: Path) -> None:
        author_skill_spec(instance_dir, loading="asap")
        loading_errors = [e for e in _errors(validate_instance_skills(instance_dir)) if "skills.loading" in e]
        assert len(loading_errors) == 1
        assert "'eager'" in loading_errors[0]
        assert "'lazy'" in loading_errors[0]

    def test_missing_field_is_not_an_error(self, instance_dir: Path) -> None:
        # No ``loading`` authored in the spec -- the assembler defaults to
        # eager and the validator must not flag the absence.
        assert not any("skills.loading" in e.message for e in validate_instance_skills(instance_dir))


class TestDuplicateSkillIds:
    """A directory name is unique on a regular filesystem, but the validator
    still emits a ``duplicate skill_id`` error if a layout produces a
    collision -- legacy domains, case-insensitive filesystems or symlinked
    instance trees.  Tests target the helper pair so the duplicate-emit
    branch is directly exercised."""

    def test_unique_layout_emits_no_errors(self, instance_dir: Path) -> None:
        errors = validate_instance_skills(instance_dir)
        assert not any("duplicate skill_id" in e.message for e in errors)

    def test_gather_keys_on_directory_names(self, instance_dir: Path) -> None:
        seen = _gather_skill_id_paths(instance_dir)
        assert set(seen) == {"alpha-skill", "beta-skill"}
        assert all(len(paths) == 1 for paths in seen.values())

    def test_emit_flags_collision_with_offending_paths(self) -> None:
        seen = {
            "alpha": ["skills/alpha", "skills/d1/alpha"],
            "lone": ["skills/lone"],
        }
        errors: list[ValidationError] = []
        _emit_duplicate_skill_id_errors(seen, errors)
        assert len(errors) == 1
        msg = errors[0].message
        assert "'alpha'" in msg
        assert "skills/alpha" in msg
        assert "skills/d1/alpha" in msg

    def test_emit_is_silent_when_no_duplicates(self) -> None:
        errors: list[ValidationError] = []
        _emit_duplicate_skill_id_errors({"a": ["p1"], "b": ["p2"]}, errors)
        assert errors == []
