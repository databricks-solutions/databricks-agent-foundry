"""
Genie Space Assembler
=====================
Reads decomposed human-editable files from a Genie Space instance directory
and compiles them into _build/api_payload.json (ready for the REST Management API).

Usage:
    python -m bricks.genie.scripts.assemble --instance <name> [--target <dev|prod>] [--vars key=val ...]
    python bricks/genie/scripts/assemble.py --instance <name> [--target <dev|prod>]

The instance directory is expected at: instances/<app>/genie_spaces/<name>/
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, cast

import yaml

from agent_foundry.bricks import load_brick_variables, resolve_sub_instances
from agent_foundry.config import parse_yaml_file
from agent_foundry.variables import add_project_root_arg, substitute, substitute_obj

# ---------------------------------------------------------------------------
# ID generation
# ---------------------------------------------------------------------------


def generate_hex_id(seed: str) -> str:
    """Generate a deterministic 32-char lowercase hex ID from a seed string.

    Uses a SHA-256 hash truncated to 16 bytes so the same file path always
    produces the same ID, making assembly idempotent.
    """
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return digest[:32]


# ---------------------------------------------------------------------------
# File parsers
# ---------------------------------------------------------------------------


def parse_sql_file(path: Path) -> dict[str, Any]:
    """Parse a SQL file with optional YAML frontmatter.

    Expected format:
        ---
        question: "..."
        usage_guidance: "..."
        parameters: [...]
        ---
        SELECT ...

    Returns dict with keys: question, sql, and optional usage_guidance/parameters.
    """
    text = path.read_text(encoding="utf-8")
    frontmatter: dict[str, Any] = {}
    sql_body = text

    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            frontmatter = yaml.safe_load(parts[1]) or {}
            sql_body = parts[2].strip()

    return {**frontmatter, "sql": sql_body}


def parse_markdown_instructions(path: Path) -> str:
    """Read text instructions from a Markdown file.

    Strips the leading heading (## General Instructions) if present,
    and any HTML comments.
    """
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    text = re.sub(r"^##\s+.*$", "", text, flags=re.MULTILINE)
    return text.strip()


# ---------------------------------------------------------------------------
# Variable substitution
# ---------------------------------------------------------------------------


def _backtick_quote_join_sql(sql: str, left_alias: str, right_alias: str) -> str:
    """Ensure join condition references are backtick-quoted as the API requires.

    Transforms ``alias.column`` to `` `alias`.`column` `` for both the left and
    right aliases.  Already-quoted references are left untouched.
    """
    import re

    for alias in (left_alias, right_alias):
        escaped = re.escape(alias)
        sql = re.sub(
            rf"(?<!`)(?:{escaped})\.(?!`)(\w+)",
            rf"`{alias}`.`\1`",
            sql,
        )
    return sql


resolve_variables = substitute
resolve_variables_in_obj = substitute_obj


# ---------------------------------------------------------------------------
# Core assembly
# ---------------------------------------------------------------------------


def _to_string_array(text: str) -> list[str]:
    """Convert a multi-line string into the API's string-array format.

    The API represents long text as arrays of strings (one per line).
    """
    if not text:
        return []
    lines = text.split("\n")
    result = []
    for i, line in enumerate(lines):
        if i < len(lines) - 1:
            result.append(line + "\n")
        elif line:
            result.append(line)
    return result


def assemble_serialized_space(instance_dir: Path, seed_prefix: str = "") -> dict[str, Any]:
    """Assemble all decomposed files into the serialized_space JSON structure.

    Args:
        instance_dir: Path to the instance directory containing space.yml etc.
        seed_prefix: Prefix for deterministic ID generation (typically instance name).

    Returns:
        The serialized_space dict (not yet JSON-encoded).
    """
    space: dict[str, Any] = {"version": 2}

    # -- config.sample_questions -----------------------------------------------
    sample_q_data = parse_yaml_file(instance_dir / "config" / "sample_questions.yml")
    questions_raw = sample_q_data.get("questions", [])
    sample_questions = []
    for i, q in enumerate(questions_raw):
        q_text = q if isinstance(q, str) else q.get("question", "")
        sample_questions.append(
            {
                "id": generate_hex_id(f"{seed_prefix}:sample_question:{i}:{q_text}"),
                "question": [q_text],
            }
        )
    # ``x["id"]`` is always the hex string set above; mypy infers
    # the dict value type as ``str | Sequence[Any]`` (because of the
    # ``"question": [...]`` neighbour) so cast to satisfy ``sort``'s
    # ``SupportsDunderLT`` key contract.
    sample_questions.sort(key=lambda x: cast("str", x["id"]))
    if sample_questions:
        space["config"] = {"sample_questions": sample_questions}

    # -- data_sources ----------------------------------------------------------
    tables_data = parse_yaml_file(instance_dir / "data_sources" / "tables.yml")
    tables_raw = tables_data.get("tables", [])
    tables = []
    for t in tables_raw:
        table_entry: dict[str, Any] = {"identifier": t["identifier"]}
        if "description" in t:
            table_entry["description"] = [t["description"]] if isinstance(t["description"], str) else t["description"]
        if "column_configs" in t and t["column_configs"]:
            cols = []
            for c in t["column_configs"]:
                col: dict[str, Any] = {"column_name": c["column_name"]}
                for flag in (
                    "enable_entity_matching",
                    "enable_format_assistance",
                    "build_value_dictionary",
                    "get_example_values",
                    "exclude",
                ):
                    if c.get(flag):
                        col[flag] = True
                if "description" in c:
                    col["description"] = [c["description"]] if isinstance(c["description"], str) else c["description"]
                if "synonyms" in c:
                    col["synonyms"] = c["synonyms"]
                cols.append(col)
            cols.sort(key=lambda x: x["column_name"])
            table_entry["column_configs"] = cols
        tables.append(table_entry)
    tables.sort(key=lambda x: x["identifier"])

    metric_views_data = parse_yaml_file(instance_dir / "data_sources" / "metric_views.yml")
    metric_views_raw = metric_views_data.get("metric_views", [])
    metric_views = []
    for mv in metric_views_raw:
        mv_entry: dict[str, Any] = {"identifier": mv["identifier"]}
        if "description" in mv:
            mv_entry["description"] = [mv["description"]] if isinstance(mv["description"], str) else mv["description"]
        if "column_configs" in mv and mv["column_configs"]:
            cols = []
            for c in mv["column_configs"]:
                # Annotation lives on the first ``col`` declaration above
                # (in the ``tables_raw`` loop); re-annotating here trips
                # mypy ``no-redef`` at module scope even though the loop
                # bodies are otherwise independent.
                col = {"column_name": c["column_name"]}
                for flag in ("enable_entity_matching", "enable_format_assistance", "exclude"):
                    if c.get(flag):
                        col[flag] = True
                if "description" in c:
                    col["description"] = [c["description"]] if isinstance(c["description"], str) else c["description"]
                cols.append(col)
            cols.sort(key=lambda x: x["column_name"])
            mv_entry["column_configs"] = cols
        metric_views.append(mv_entry)
    metric_views.sort(key=lambda x: x["identifier"])

    data_sources: dict[str, Any] = {}
    if tables:
        data_sources["tables"] = tables
    if metric_views:
        data_sources["metric_views"] = metric_views
    if data_sources:
        space["data_sources"] = data_sources

    # -- instructions ----------------------------------------------------------
    instructions: dict[str, Any] = {}

    # text_instructions
    instr_text = parse_markdown_instructions(instance_dir / "instructions" / "text_instructions.md")
    if instr_text:
        instructions["text_instructions"] = [
            {
                "id": generate_hex_id(f"{seed_prefix}:text_instruction:0"),
                "content": _to_string_array(instr_text),
            }
        ]

    # example_question_sqls
    sql_examples_dir = instance_dir / "instructions" / "sql_examples"
    example_sqls = []
    if sql_examples_dir.exists():
        for sql_file in sorted(sql_examples_dir.glob("*.sql")):
            if sql_file.name.startswith("_"):
                continue
            parsed = parse_sql_file(sql_file)
            question = parsed.get("question", "")
            if not question:
                continue
            entry: dict[str, Any] = {
                "id": generate_hex_id(f"{seed_prefix}:sql_example:{sql_file.stem}"),
                "question": [question],
                "sql": _to_string_array(parsed["sql"]),
            }
            if "usage_guidance" in parsed:
                entry["usage_guidance"] = [parsed["usage_guidance"]]
            if "parameters" in parsed and parsed["parameters"]:
                params = []
                for p in parsed["parameters"]:
                    param: dict[str, Any] = {"name": p["name"]}
                    if "type_hint" in p:
                        param["type_hint"] = p["type_hint"]
                    if "description" in p:
                        desc = p["description"]
                        param["description"] = [desc] if isinstance(desc, str) else desc
                    if "default_value" in p:
                        val = p["default_value"]
                        param["default_value"] = {"values": val if isinstance(val, list) else [val]}
                    params.append(param)
                entry["parameters"] = params
            example_sqls.append(entry)
    example_sqls.sort(key=lambda x: x["id"])
    if example_sqls:
        instructions["example_question_sqls"] = example_sqls

    # sql_functions
    funcs_data = parse_yaml_file(instance_dir / "instructions" / "sql_functions.yml")
    sql_functions_raw = funcs_data.get("sql_functions", [])
    sql_functions = []
    for i, f in enumerate(sql_functions_raw):
        identifier = f if isinstance(f, str) else f.get("identifier", "")
        if not identifier:
            continue
        sql_functions.append(
            {
                "id": generate_hex_id(f"{seed_prefix}:sql_function:{i}:{identifier}"),
                "identifier": identifier,
            }
        )
    sql_functions.sort(key=lambda x: (x["id"], x["identifier"]))
    if sql_functions:
        instructions["sql_functions"] = sql_functions

    # join_specs
    joins_data = parse_yaml_file(instance_dir / "instructions" / "join_specs.yml")
    join_specs_raw = joins_data.get("join_specs", [])
    join_specs = []
    for i, j in enumerate(join_specs_raw):
        left = j["left"]
        right = j["right"]
        left_alias = left.get("alias", left["identifier"].split(".")[-1])
        right_alias = right.get("alias", right["identifier"].split(".")[-1])

        raw_sql = j["sql"]
        if isinstance(raw_sql, list):
            raw_sql = " AND ".join(raw_sql)
        sql_str = _backtick_quote_join_sql(raw_sql, left_alias, right_alias)

        rt = j.get("relationship_type", "many_to_one")
        rt_marker = f"--rt=FROM_RELATIONSHIP_TYPE_{rt.upper()}--"

        join_entry: dict[str, Any] = {
            "id": generate_hex_id(f"{seed_prefix}:join_spec:{i}:{left['identifier']}:{right['identifier']}"),
            "left": {"identifier": left["identifier"], "alias": left_alias},
            "right": {"identifier": right["identifier"], "alias": right_alias},
            "sql": [sql_str, rt_marker],
        }
        if "comment" in j:
            join_entry["comment"] = [j["comment"]] if isinstance(j["comment"], str) else j["comment"]
        if "instruction" in j:
            join_entry["instruction"] = [j["instruction"]] if isinstance(j["instruction"], str) else j["instruction"]
        join_specs.append(join_entry)
    join_specs.sort(key=lambda x: x["id"])
    if join_specs:
        instructions["join_specs"] = join_specs

    # sql_snippets
    snippets_data = parse_yaml_file(instance_dir / "instructions" / "sql_snippets.yml")
    sql_snippets: dict[str, Any] = {}

    for snippet_type in ("filters", "expressions", "measures"):
        items_raw = snippets_data.get(snippet_type, [])
        items = []
        for i, item in enumerate(items_raw):
            snippet: dict[str, Any] = {
                "id": generate_hex_id(f"{seed_prefix}:snippet:{snippet_type}:{i}:{item.get('display_name', '')}"),
                "sql": [item["sql"]] if isinstance(item["sql"], str) else item["sql"],
                "display_name": item["display_name"],
            }
            if snippet_type in ("expressions", "measures") and "alias" in item:
                snippet["alias"] = item["alias"]
            if "synonyms" in item:
                snippet["synonyms"] = item["synonyms"]
            if "comment" in item:
                comment = item["comment"]
                snippet["comment"] = [comment] if isinstance(comment, str) else comment
            if "instruction" in item:
                instr = item["instruction"]
                snippet["instruction"] = [instr] if isinstance(instr, str) else instr
            items.append(snippet)
        items.sort(key=lambda x: x["id"])
        if items:
            sql_snippets[snippet_type] = items

    if sql_snippets:
        instructions["sql_snippets"] = sql_snippets

    if instructions:
        space["instructions"] = instructions

    # -- benchmarks ------------------------------------------------------------
    bench_data = parse_yaml_file(instance_dir / "benchmarks" / "questions.yml")
    bench_raw = bench_data.get("questions", [])
    benchmarks = []
    for i, b in enumerate(bench_raw):
        q_text = b.get("question", "")
        sql_text = b.get("sql", "")
        if not q_text or not sql_text:
            continue
        benchmarks.append(
            {
                "id": generate_hex_id(f"{seed_prefix}:benchmark:{i}:{q_text}"),
                "question": [q_text],
                "answer": [
                    {
                        "format": "SQL",
                        "content": _to_string_array(sql_text),
                    }
                ],
            }
        )
    # ``x["id"]`` is always a hex string; cast to satisfy ``sort``'s
    # ``SupportsDunderLT`` contract (the dict's value union includes
    # ``Sequence[Any]`` for the SQL content list).
    benchmarks.sort(key=lambda x: cast("str", x["id"]))
    if benchmarks:
        space["benchmarks"] = {"questions": benchmarks}

    return space


def assemble_api_payload(instance_dir: Path, variables: dict[str, str] | None = None) -> dict[str, Any]:
    """Assemble the complete API payload for creating/updating a Genie Space.

    Args:
        instance_dir: Path to the instance directory.
        variables: Variable values for ${var.xxx} substitution.

    Returns:
        Dict matching the POST /api/2.0/genie/spaces request body.
    """
    variables = dict(variables or {})
    space_config = parse_yaml_file(instance_dir / "space.yml")
    instance_name = instance_dir.name

    for k, v in space_config.get("variables", {}).items():
        variables.setdefault(k, str(v))

    serialized_space = assemble_serialized_space(instance_dir, seed_prefix=instance_name)

    if variables:
        serialized_space = resolve_variables_in_obj(serialized_space, variables)

    genie_prefix = variables.get("genie_display_prefix", "")
    default_title = f"{genie_prefix}-genie-{instance_name}" if genie_prefix else instance_name

    payload: dict[str, Any] = {
        "title": space_config.get("title", default_title),
        "description": space_config.get("description", ""),
        "warehouse_id": space_config.get("warehouse_id", variables.get("warehouse_id", "")),
        "serialized_space": json.dumps(serialized_space, indent=2),
    }

    parent_path = space_config.get("parent_path", "")
    if not parent_path:
        solution_root = variables.get("solution_root", "")
        if solution_root:
            parent_path = f"/Workspace{solution_root}"
    if parent_path:
        payload["parent_path"] = parent_path

    if variables:
        payload = resolve_variables_in_obj(payload, variables)

    return payload


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Assemble a Genie Space from decomposed files")
    parser.add_argument(
        "--instance",
        required=True,
        help="'<app>/<space>', an app name when it owns a single space, or a path to either",
    )
    parser.add_argument("--target", default="dev", help="Deployment target (dev, staging, prod)")
    parser.add_argument("--vars", nargs="*", default=[], help="Extra variables as key=value pairs")
    parser.add_argument(
        "--no-resolve", action="store_true", help="Skip variable resolution (keep ${var.xxx} references)"
    )
    parser.add_argument("--output-dir", help="Override output directory (default: instance/_build/)")
    add_project_root_arg(parser)
    args = parser.parse_args(argv)

    project_root = args.project_root
    try:
        instance_dir, space_dirs = resolve_sub_instances(project_root, args.instance, "genie_spaces")
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    # One build dir per invocation, so the space has to be unambiguous.
    if len(space_dirs) > 1:
        names = ", ".join(d.name for d in space_dirs)
        print(f"Error: --instance matched several spaces ({names}); pass '<app>/<space>'", file=sys.stderr)
        return 1
    space_dir = space_dirs[0]

    if not (space_dir / "space.yml").exists():
        print(f"Error: space.yml not found in {space_dir}", file=sys.stderr)
        return 1

    build_dir = Path(args.output_dir) if args.output_dir else space_dir / "_build"
    build_dir.mkdir(parents=True, exist_ok=True)

    variables = (
        {} if args.no_resolve else load_brick_variables(project_root, args.target, instance_dir, extra_vars=args.vars)
    )

    # -- API payload (variables resolved) --------------------------------------
    try:
        if args.no_resolve:
            payload = assemble_api_payload(space_dir)
        else:
            payload = assemble_api_payload(space_dir, variables)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    payload_path = build_dir / "api_payload.json"
    payload_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  API payload: {payload_path}")

    # -- Summary ---------------------------------------------------------------
    ss = json.loads(payload["serialized_space"])
    n_tables = len(ss.get("data_sources", {}).get("tables", []))
    n_examples = len(ss.get("instructions", {}).get("example_question_sqls", []))
    n_benchmarks = len(ss.get("benchmarks", {}).get("questions", []))
    print(f"\n  Assembled: {space_dir.name}")
    print(f"    Tables:     {n_tables}")
    print(f"    SQL examples: {n_examples}")
    print(f"    Benchmarks: {n_benchmarks}")
    print(f"    Target:     {args.target}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
