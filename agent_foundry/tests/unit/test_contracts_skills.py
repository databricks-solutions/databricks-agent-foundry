"""Unit tests for ``agent_foundry.contracts.skills``."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent_foundry.contracts.skills import (
    MAX_SKILL_DESCRIPTION_CHARS,
    SkillDocument,
    SkillDocumentError,
    SkillDomainMetadata,
    SkillMetadata,
    is_skill_slug,
    parse_skill_md,
    render_skill_md,
)


class TestSkillMd:
    def test_parses_frontmatter_and_body(self) -> None:
        text = "---\nname: sql-runner\ndescription: Use when running SQL.\n---\n\n# SQL\n\nSteps.\n"
        assert parse_skill_md(text) == SkillDocument("sql-runner", "Use when running SQL.", "# SQL\n\nSteps.")

    def test_render_round_trips(self) -> None:
        text = render_skill_md("triage", "Use when: tickets", "Body\n")
        assert parse_skill_md(text) == SkillDocument("triage", "Use when: tickets", "Body")

    def test_empty_body_is_allowed_by_the_parser(self) -> None:
        assert parse_skill_md("---\nname: a\ndescription: b\n---\n").body == ""

    @pytest.mark.parametrize(
        ("text", "message"),
        [
            ("# no frontmatter\n", "frontmatter"),
            ("---\n[unclosed\n---\n", "not valid YAML"),
            ("---\n- a list\n---\n", "frontmatter"),
            ("---\ndescription: d\n---\nx", "name"),
            ("---\nname: a\ndescription: '  '\n---\nx", "description"),
            (f"---\nname: a\ndescription: {'d' * (MAX_SKILL_DESCRIPTION_CHARS + 1)}\n---\nx", "over"),
        ],
    )
    def test_rejects(self, text: str, message: str) -> None:
        with pytest.raises(SkillDocumentError, match=message):
            parse_skill_md(text)

    @pytest.mark.parametrize("value", ["sql-runner", "a", "a1-b2"])
    def test_slug_accepts(self, value: str) -> None:
        assert is_skill_slug(value)

    @pytest.mark.parametrize("value", ["sql_runner", "Sql", "-a", "a--b", "a-", "", "a" * 65])
    def test_slug_rejects(self, value: str) -> None:
        assert not is_skill_slug(value)

    def test_slug_honours_a_shorter_cap(self) -> None:
        assert not is_skill_slug("a" * 41, max_chars=40)


class TestSkillMetadata:
    def test_minimal(self) -> None:
        s = SkillMetadata(name="x")
        assert s.description == ""
        assert s.triggers == []
        assert s.phase == 999
        assert s.tools == []

    def test_full(self) -> None:
        s = SkillMetadata(
            name="catalog_explorer",
            description="Query UC metadata",
            triggers=["how many tables"],
            phase=1,
            tools=["catalog_genie", "format_byte_size"],
        )
        assert s.phase == 1
        assert s.tools == ["catalog_genie", "format_byte_size"]

    def test_missing_name_raises(self) -> None:
        with pytest.raises(ValidationError):
            SkillMetadata()  # type: ignore[call-arg]


class TestSkillDomainMetadata:
    def test_minimal(self) -> None:
        d = SkillDomainMetadata(name="analytics")
        assert d.description == ""

    def test_missing_name_raises(self) -> None:
        with pytest.raises(ValidationError):
            SkillDomainMetadata()  # type: ignore[call-arg]
