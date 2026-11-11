"""Tests for the Genie Space validator (no workspace required)."""

from __future__ import annotations

import copy
from typing import Any

from bricks.genie.scripts.validate import validate_serialized_space


class TestValidateSmokeFicture:
    """The smoke fixture should pass validation with zero errors."""

    def test_no_errors(self, assembled_space: dict[str, Any]) -> None:
        errors = validate_serialized_space(assembled_space)
        assert len(errors) == 0, f"Unexpected errors: {[str(e) for e in errors]}"


class TestValidateBadInputs:
    """Verify the validator catches known-bad inputs."""

    def test_missing_version(self, assembled_space: dict[str, Any]) -> None:
        bad = copy.deepcopy(assembled_space)
        del bad["version"]
        errors = validate_serialized_space(bad)
        assert any("version" in e.path for e in errors)

    def test_wrong_version(self, assembled_space: dict[str, Any]) -> None:
        bad = copy.deepcopy(assembled_space)
        bad["version"] = 1
        errors = validate_serialized_space(bad)
        assert any("version" in e.path and "must be 2" in e.message for e in errors)

    def test_bad_table_identifier(self, assembled_space: dict[str, Any]) -> None:
        bad = copy.deepcopy(assembled_space)
        bad["data_sources"]["tables"][0]["identifier"] = "no_dots_here"
        errors = validate_serialized_space(bad)
        assert any("three-level" in e.message for e in errors)

    def test_unsorted_tables(self, assembled_space: dict[str, Any]) -> None:
        bad = copy.deepcopy(assembled_space)
        tables = bad["data_sources"]["tables"]
        if len(tables) >= 2:
            tables[0], tables[1] = tables[1], tables[0]
        errors = validate_serialized_space(bad)
        assert any("sorted" in e.message for e in errors)

    def test_bad_hex_id(self, assembled_space: dict[str, Any]) -> None:
        bad = copy.deepcopy(assembled_space)
        bad["config"]["sample_questions"][0]["id"] = "NOT_A_HEX_ID"
        errors = validate_serialized_space(bad)
        assert any("32-char lowercase hex" in e.message for e in errors)

    def test_duplicate_question_ids(self, assembled_space: dict[str, Any]) -> None:
        bad = copy.deepcopy(assembled_space)
        questions = bad["config"]["sample_questions"]
        if len(questions) >= 2:
            questions[1]["id"] = questions[0]["id"]
        errors = validate_serialized_space(bad)
        assert any("duplicate" in e.message for e in errors)

    def test_multiple_text_instructions(self, assembled_space: dict[str, Any]) -> None:
        bad = copy.deepcopy(assembled_space)
        ti = bad["instructions"]["text_instructions"]
        bad["instructions"]["text_instructions"] = ti + [{"id": "a" * 32, "content": ["second"]}]
        errors = validate_serialized_space(bad)
        assert any("at most 1" in e.message for e in errors)

    def test_benchmark_wrong_format(self, assembled_space: dict[str, Any]) -> None:
        bad = copy.deepcopy(assembled_space)
        bad["benchmarks"]["questions"][0]["answer"][0]["format"] = "TEXT"
        errors = validate_serialized_space(bad)
        assert any("format must be 'SQL'" in e.message for e in errors)

    def test_empty_snippet_sql(self, assembled_space: dict[str, Any]) -> None:
        bad = copy.deepcopy(assembled_space)
        measures = bad["instructions"]["sql_snippets"]["measures"]
        measures[0]["sql"] = []
        errors = validate_serialized_space(bad)
        assert any("sql" in e.path and "not be empty" in e.message for e in errors)
