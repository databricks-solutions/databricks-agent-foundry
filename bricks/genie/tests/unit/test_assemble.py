"""Tests for the Genie Space assembler (no workspace required)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

HEX_ID_RE = re.compile(r"^[0-9a-f]{32}$")


class TestAssembleStructure:
    """Verify the assembled serialized_space has correct structure."""

    def test_version_is_2(self, assembled_space: dict[str, Any]) -> None:
        assert assembled_space["version"] == 2

    def test_has_data_sources(self, assembled_space: dict[str, Any]) -> None:
        ds = assembled_space.get("data_sources", {})
        assert "tables" in ds
        assert len(ds["tables"]) == 2

    def test_tables_are_sorted(self, assembled_space: dict[str, Any]) -> None:
        tables = assembled_space["data_sources"]["tables"]
        identifiers = [t["identifier"] for t in tables]
        assert identifiers == sorted(identifiers)

    def test_table_identifiers_are_three_level(self, assembled_space: dict[str, Any]) -> None:
        for t in assembled_space["data_sources"]["tables"]:
            parts = t["identifier"].split(".")
            assert len(parts) == 3, f"Expected three-level identifier, got: {t['identifier']}"

    def test_column_configs_are_sorted(self, assembled_space: dict[str, Any]) -> None:
        for t in assembled_space["data_sources"]["tables"]:
            if "column_configs" in t:
                names = [c["column_name"] for c in t["column_configs"]]
                assert names == sorted(names), f"Column configs not sorted in {t['identifier']}"


class TestAssembleIDs:
    """Verify all IDs are valid 32-char hex and sorted."""

    def _collect_ids(self, items: list[dict[str, Any]]) -> list[str]:
        return [item["id"] for item in items]

    def test_sample_question_ids(self, assembled_space: dict[str, Any]) -> None:
        questions = assembled_space.get("config", {}).get("sample_questions", [])
        assert len(questions) >= 2
        for q in questions:
            assert HEX_ID_RE.match(q["id"]), f"Invalid ID: {q['id']}"
        ids = self._collect_ids(questions)
        assert ids == sorted(ids), "sample_questions not sorted by id"

    def test_text_instruction_id(self, assembled_space: dict[str, Any]) -> None:
        instrs = assembled_space.get("instructions", {}).get("text_instructions", [])
        assert len(instrs) == 1
        assert HEX_ID_RE.match(instrs[0]["id"])

    def test_sql_example_ids(self, assembled_space: dict[str, Any]) -> None:
        examples = assembled_space.get("instructions", {}).get("example_question_sqls", [])
        assert len(examples) == 2
        for ex in examples:
            assert HEX_ID_RE.match(ex["id"]), f"Invalid ID: {ex['id']}"
        ids = self._collect_ids(examples)
        assert ids == sorted(ids), "example_question_sqls not sorted by id"

    def test_benchmark_ids(self, assembled_space: dict[str, Any]) -> None:
        questions = assembled_space.get("benchmarks", {}).get("questions", [])
        assert len(questions) == 2
        for q in questions:
            assert HEX_ID_RE.match(q["id"]), f"Invalid ID: {q['id']}"

    def test_ids_are_deterministic(self, smoke_instance_dir: Path) -> None:
        """Same input should produce same IDs every time."""
        from bricks.genie.scripts.assemble import assemble_serialized_space

        run1 = assemble_serialized_space(smoke_instance_dir, seed_prefix="genie_smoke")
        run2 = assemble_serialized_space(smoke_instance_dir, seed_prefix="genie_smoke")

        ids1 = [q["id"] for q in run1["config"]["sample_questions"]]
        ids2 = [q["id"] for q in run2["config"]["sample_questions"]]
        assert ids1 == ids2


class TestAssembleSQLExamples:
    """Verify SQL examples are parsed correctly from frontmatter."""

    def test_question_and_sql_present(self, assembled_space: dict[str, Any]) -> None:
        examples = assembled_space["instructions"]["example_question_sqls"]
        for ex in examples:
            assert ex["question"], "question should not be empty"
            assert ex["sql"], "sql should not be empty"
            assert isinstance(ex["question"], list)
            assert isinstance(ex["sql"], list)

    def test_sql_contains_expected_content(self, assembled_space: dict[str, Any]) -> None:
        examples = assembled_space["instructions"]["example_question_sqls"]
        all_sql = " ".join(" ".join(ex["sql"]) for ex in examples)
        assert "information_schema" in all_sql


class TestAssembleBenchmarks:
    """Verify benchmark structure matches API requirements."""

    def test_benchmarks_have_sql_answers(self, assembled_space: dict[str, Any]) -> None:
        questions = assembled_space["benchmarks"]["questions"]
        for q in questions:
            assert len(q["answer"]) == 1
            assert q["answer"][0]["format"] == "SQL"
            assert q["answer"][0]["content"]


class TestAssemblePayload:
    """Verify the full API payload structure."""

    def test_payload_has_required_keys(self, assembled_payload: dict[str, Any]) -> None:
        assert "title" in assembled_payload
        assert "description" in assembled_payload
        assert "warehouse_id" in assembled_payload
        assert "serialized_space" in assembled_payload

    def test_serialized_space_is_valid_json(self, assembled_payload: dict[str, Any]) -> None:
        ss = json.loads(assembled_payload["serialized_space"])
        assert ss["version"] == 2

    def test_join_specs_present(self, assembled_space: dict[str, Any]) -> None:
        joins = assembled_space.get("instructions", {}).get("join_specs", [])
        assert len(joins) == 1
        assert "left" in joins[0]
        assert "right" in joins[0]
        assert "sql" in joins[0]

    def test_sql_snippets_measures(self, assembled_space: dict[str, Any]) -> None:
        snippets = assembled_space.get("instructions", {}).get("sql_snippets", {})
        measures = snippets.get("measures", [])
        assert len(measures) == 1
        assert measures[0]["display_name"] == "table count"
