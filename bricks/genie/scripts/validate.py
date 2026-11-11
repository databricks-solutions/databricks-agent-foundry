"""
Genie Space Validator
=====================
Validates a serialized_space JSON (or an assembled instance) against the
Databricks Genie API validation rules without deploying.

Rules enforced (from API docs):
    - version field: must be 2
    - IDs: 32-char lowercase hex
    - Sorting: arrays pre-sorted by required keys
    - Uniqueness: no duplicate IDs across collections
    - Size limits: strings <=25,000 chars, arrays <=10,000 items
    - Max 1 text_instruction
    - Table identifiers: three-level namespace (catalog.schema.table)
    - Benchmark answers: exactly one answer with format=SQL
    - SQL snippets: sql fields must not be empty

Usage:
    python bricks/genie/scripts/validate.py --instance <name>
    python bricks/genie/scripts/validate.py --file <serialized_space.json>
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from agent_foundry.bricks import resolve_sub_instances
from agent_foundry.variables import add_project_root_arg
from bricks.genie.scripts.assemble import assemble_serialized_space

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

HEX_ID_RE = re.compile(r"^[0-9a-f]{32}$")
TABLE_ID_RE = re.compile(r"^[^.]+\.[^.]+\.[^.]+$")

MAX_STRING_LENGTH = 25_000
MAX_ARRAY_SIZE = 10_000


class ValidationError:
    def __init__(self, path: str, message: str, severity: str = "error"):
        self.path = path
        self.message = message
        self.severity = severity

    def __str__(self) -> str:
        icon = "ERROR" if self.severity == "error" else "WARN"
        return f"[{icon}] {self.path}: {self.message}"


def validate_serialized_space(space: dict[str, Any]) -> list[ValidationError]:
    """Validate a serialized_space dict against all API rules.

    Returns a list of ValidationError objects. Empty list = valid.
    """
    errors: list[ValidationError] = []

    _validate_version(space, errors)
    _validate_data_sources(space.get("data_sources", {}), errors)
    _validate_config(space.get("config", {}), errors)
    _validate_instructions(space.get("instructions", {}), errors)
    _validate_benchmarks(space.get("benchmarks", {}), errors)
    _validate_id_uniqueness(space, errors)

    return errors


def _validate_version(space: dict[str, Any], errors: list[ValidationError]) -> None:
    version = space.get("version")
    if version is None:
        errors.append(ValidationError("version", "version field is required"))
    elif version != 2:
        errors.append(ValidationError("version", f"version must be 2, got {version}"))


def _validate_hex_id(value: str, path: str, errors: list[ValidationError]) -> None:
    if not HEX_ID_RE.match(value):
        errors.append(ValidationError(path, f"ID must be 32-char lowercase hex, got '{value}'"))


def _validate_sorted(items: list[dict[str, Any]], key: str, path: str, errors: list[ValidationError]) -> None:
    keys = [item.get(key, "") for item in items]
    if keys != sorted(keys):
        errors.append(ValidationError(path, f"array must be sorted by '{key}'"))


def _validate_string_length(value: Any, path: str, errors: list[ValidationError]) -> None:
    if isinstance(value, str) and len(value) > MAX_STRING_LENGTH:
        errors.append(ValidationError(path, f"string exceeds {MAX_STRING_LENGTH} chars ({len(value)})"))
    elif isinstance(value, list):
        for i, item in enumerate(value):
            _validate_string_length(item, f"{path}[{i}]", errors)


def _validate_array_size(items: list[Any], path: str, errors: list[ValidationError]) -> None:
    if len(items) > MAX_ARRAY_SIZE:
        errors.append(ValidationError(path, f"array exceeds {MAX_ARRAY_SIZE} items ({len(items)})"))


def _validate_table_identifier(identifier: str, path: str, errors: list[ValidationError]) -> None:
    if not TABLE_ID_RE.match(identifier):
        errors.append(
            ValidationError(
                path,
                f"table identifier must be three-level (catalog.schema.table), got '{identifier}'",
            )
        )


def _validate_data_sources(ds: dict[str, Any], errors: list[ValidationError]) -> None:
    tables = ds.get("tables", [])
    _validate_array_size(tables, "data_sources.tables", errors)
    _validate_sorted(tables, "identifier", "data_sources.tables", errors)
    for i, t in enumerate(tables):
        path = f"data_sources.tables[{i}]"
        _validate_table_identifier(t.get("identifier", ""), f"{path}.identifier", errors)
        if "description" in t:
            _validate_string_length(t["description"], f"{path}.description", errors)
        cols = t.get("column_configs", [])
        _validate_sorted(cols, "column_name", f"{path}.column_configs", errors)

    mvs = ds.get("metric_views", [])
    _validate_array_size(mvs, "data_sources.metric_views", errors)
    _validate_sorted(mvs, "identifier", "data_sources.metric_views", errors)
    for i, mv in enumerate(mvs):
        path = f"data_sources.metric_views[{i}]"
        _validate_table_identifier(mv.get("identifier", ""), f"{path}.identifier", errors)


def _validate_config(config: dict[str, Any], errors: list[ValidationError]) -> None:
    questions = config.get("sample_questions", [])
    _validate_array_size(questions, "config.sample_questions", errors)
    _validate_sorted(questions, "id", "config.sample_questions", errors)
    for i, q in enumerate(questions):
        _validate_hex_id(q.get("id", ""), f"config.sample_questions[{i}].id", errors)


def _validate_instructions(instr: dict[str, Any], errors: list[ValidationError]) -> None:
    # text_instructions: max 1
    text_instrs = instr.get("text_instructions", [])
    if len(text_instrs) > 1:
        errors.append(
            ValidationError(
                "instructions.text_instructions",
                f"at most 1 text instruction allowed, got {len(text_instrs)}",
            )
        )
    for i, ti in enumerate(text_instrs):
        _validate_hex_id(ti.get("id", ""), f"instructions.text_instructions[{i}].id", errors)
        _validate_string_length(ti.get("content"), f"instructions.text_instructions[{i}].content", errors)

    # example_question_sqls
    examples = instr.get("example_question_sqls", [])
    _validate_array_size(examples, "instructions.example_question_sqls", errors)
    _validate_sorted(examples, "id", "instructions.example_question_sqls", errors)
    for i, ex in enumerate(examples):
        path = f"instructions.example_question_sqls[{i}]"
        _validate_hex_id(ex.get("id", ""), f"{path}.id", errors)
        _validate_string_length(ex.get("sql"), f"{path}.sql", errors)

    # sql_functions
    funcs = instr.get("sql_functions", [])
    _validate_sorted(funcs, "id", "instructions.sql_functions", errors)
    for i, f in enumerate(funcs):
        _validate_hex_id(f.get("id", ""), f"instructions.sql_functions[{i}].id", errors)

    # join_specs
    joins = instr.get("join_specs", [])
    _validate_array_size(joins, "instructions.join_specs", errors)
    _validate_sorted(joins, "id", "instructions.join_specs", errors)
    for i, j in enumerate(joins):
        path = f"instructions.join_specs[{i}]"
        _validate_hex_id(j.get("id", ""), f"{path}.id", errors)
        sql = j.get("sql", [])
        if not sql or (isinstance(sql, list) and not any(sql)):
            errors.append(ValidationError(f"{path}.sql", "join sql must not be empty"))

    # sql_snippets
    snippets = instr.get("sql_snippets", {})
    for snippet_type in ("filters", "expressions", "measures"):
        items = snippets.get(snippet_type, [])
        base_path = f"instructions.sql_snippets.{snippet_type}"
        _validate_array_size(items, base_path, errors)
        _validate_sorted(items, "id", base_path, errors)
        for i, item in enumerate(items):
            path = f"{base_path}[{i}]"
            _validate_hex_id(item.get("id", ""), f"{path}.id", errors)
            sql = item.get("sql", [])
            if not sql or (isinstance(sql, list) and not any(sql)):
                errors.append(ValidationError(f"{path}.sql", "snippet sql must not be empty"))


def _validate_benchmarks(bench: dict[str, Any], errors: list[ValidationError]) -> None:
    questions = bench.get("questions", [])
    _validate_array_size(questions, "benchmarks.questions", errors)
    _validate_sorted(questions, "id", "benchmarks.questions", errors)
    for i, q in enumerate(questions):
        path = f"benchmarks.questions[{i}]"
        _validate_hex_id(q.get("id", ""), f"{path}.id", errors)
        answers = q.get("answer", [])
        if len(answers) != 1:
            errors.append(ValidationError(f"{path}.answer", f"exactly 1 answer required, got {len(answers)}"))
        elif answers[0].get("format") != "SQL":
            errors.append(ValidationError(f"{path}.answer[0].format", "format must be 'SQL'"))


def _validate_id_uniqueness(space: dict[str, Any], errors: list[ValidationError]) -> None:
    """Check that IDs are unique across related collections."""
    # Question IDs (sample_questions + benchmark questions)
    question_ids: list[str] = []
    for q in space.get("config", {}).get("sample_questions", []):
        question_ids.append(q.get("id", ""))
    for q in space.get("benchmarks", {}).get("questions", []):
        question_ids.append(q.get("id", ""))
    _check_duplicates(question_ids, "question IDs (sample + benchmark)", errors)

    # Instruction IDs (text_instructions + example_sqls + sql_functions + join_specs + all snippets)
    instr_ids: list[str] = []
    instr = space.get("instructions", {})
    for ti in instr.get("text_instructions", []):
        instr_ids.append(ti.get("id", ""))
    for ex in instr.get("example_question_sqls", []):
        instr_ids.append(ex.get("id", ""))
    for f in instr.get("sql_functions", []):
        instr_ids.append(f.get("id", ""))
    for j in instr.get("join_specs", []):
        instr_ids.append(j.get("id", ""))
    snippets = instr.get("sql_snippets", {})
    for snippet_type in ("filters", "expressions", "measures"):
        for item in snippets.get(snippet_type, []):
            instr_ids.append(item.get("id", ""))
    _check_duplicates(instr_ids, "instruction IDs", errors)

    # Column uniqueness: (table_identifier, column_name) pairs
    col_keys: list[str] = []
    ds = space.get("data_sources", {})
    for t in ds.get("tables", []):
        for c in t.get("column_configs", []):
            col_keys.append(f"{t['identifier']}.{c['column_name']}")
    for mv in ds.get("metric_views", []):
        for c in mv.get("column_configs", []):
            col_keys.append(f"{mv['identifier']}.{c['column_name']}")
    _check_duplicates(col_keys, "column (table, column_name) pairs", errors)


def _check_duplicates(items: list[str], label: str, errors: list[ValidationError]) -> None:
    seen: set[str] = set()
    for item in items:
        if item in seen:
            errors.append(ValidationError(label, f"duplicate: '{item}'"))
        seen.add(item)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a Genie Space definition")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--instance",
        help="App name (validates every Genie Space), '<app>/<space>', or a path to either",
    )
    group.add_argument("--file", help="Path to a serialized_space JSON file")
    add_project_root_arg(parser)
    args = parser.parse_args(argv)

    if args.file:
        path = Path(args.file)
        if not path.exists():
            print(f"Error: file not found: {path}", file=sys.stderr)
            return 1
        spaces = [json.loads(path.read_text(encoding="utf-8"))]
    else:
        try:
            _, space_dirs = resolve_sub_instances(args.project_root, args.instance, "genie_spaces")
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        spaces = [assemble_serialized_space(d, seed_prefix=d.name) for d in space_dirs]

    errors = [error for space in spaces for error in validate_serialized_space(space)]

    if not errors:
        print("Validation passed: no errors found.")
        return 0

    error_count = sum(1 for e in errors if e.severity == "error")
    warn_count = sum(1 for e in errors if e.severity != "error")

    for e in errors:
        print(str(e))

    print(f"\n{error_count} error(s), {warn_count} warning(s)")
    return 1 if error_count > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
