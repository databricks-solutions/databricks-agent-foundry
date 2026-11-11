"""
Genie Space Export / Decompose
==============================
Fetches an existing Genie Space from the Management API and writes it
into the decomposed file structure used by this repo. Essential for
bootstrapping: take a UI-built space and import it into version control.

Usage:
    python bricks/genie/scripts/export.py --space-id <id> --output <dir>
    python bricks/genie/scripts/export.py --space-id <id> --instance <name>
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, cast

import yaml

from agent_foundry.bricks import sub_instance_dir
from agent_foundry.variables import add_project_root_arg

# ---------------------------------------------------------------------------
# API fetch
# ---------------------------------------------------------------------------


def _get_client(
    variables: dict[str, str] | None = None,
    *,
    ws: Any | None = None,
    construct: bool = False,
) -> Any | None:
    if ws is not None:
        return ws
    if not construct:
        return None
    from agent_foundry.workspace_client import prefer_workspace_client

    return prefer_workspace_client(variables)


def fetch_space(client: Any, space_id: str) -> dict[str, Any]:
    """GET /api/2.0/genie/spaces/{space_id}?include_serialized_space=true"""
    # ``api_client.do`` is typed as ``Any``; the Genie spaces API contract
    # returns a mapping.
    return cast(
        dict[str, Any],
        client.api_client.do(
            "GET",
            f"/api/2.0/genie/spaces/{space_id}",
            query={"include_serialized_space": "true"},
        ),
    )


# ---------------------------------------------------------------------------
# Decompose helpers
# ---------------------------------------------------------------------------


def _from_string_array(arr: list[str] | str | None) -> str:
    """Convert the API's string-array format back to a single string."""
    if arr is None:
        return ""
    if isinstance(arr, str):
        return arr
    return "".join(arr).rstrip("\n")


def _sanitize_filename(text: str, max_len: int = 60) -> str:
    """Create a filesystem-safe filename from question text."""
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9\s]", "", text)
    text = re.sub(r"\s+", "_", text)
    return text[:max_len]


def decompose_space(api_response: dict[str, Any], output_dir: Path) -> None:
    """Write an API response into the decomposed file structure.

    Args:
        api_response: Full response from GET /api/2.0/genie/spaces/{id}
        output_dir: Target directory (e.g. instances/<app>/genie_spaces/<name>/)
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    serialized_raw = api_response.get("serialized_space", "{}")
    ss = json.loads(serialized_raw) if isinstance(serialized_raw, str) else serialized_raw

    # -- space.yml -------------------------------------------------------------
    space_yml: dict[str, Any] = {
        "title": api_response.get("title", ""),
        "description": api_response.get("description", ""),
        "warehouse_id": api_response.get("warehouse_id", ""),
    }
    _write_yaml(output_dir / "space.yml", space_yml, header="# Genie Space Configuration (exported)")

    # -- data_sources/tables.yml -----------------------------------------------
    ds = ss.get("data_sources", {})
    tables_raw = ds.get("tables", [])
    tables = []
    for t in tables_raw:
        entry: dict[str, Any] = {"identifier": t["identifier"]}
        desc = _from_string_array(t.get("description"))
        if desc:
            entry["description"] = desc
        if "column_configs" in t:
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
                desc = _from_string_array(c.get("description"))
                if desc:
                    col["description"] = desc
                if "synonyms" in c:
                    col["synonyms"] = c["synonyms"]
                cols.append(col)
            entry["column_configs"] = cols
        tables.append(entry)

    (output_dir / "data_sources").mkdir(exist_ok=True)
    _write_yaml(output_dir / "data_sources" / "tables.yml", {"tables": tables}, header="# Table definitions (exported)")

    # -- data_sources/metric_views.yml -----------------------------------------
    mvs_raw = ds.get("metric_views", [])
    mvs = []
    for mv in mvs_raw:
        entry = {"identifier": mv["identifier"]}
        desc = _from_string_array(mv.get("description"))
        if desc:
            entry["description"] = desc
        if "column_configs" in mv:
            cols = []
            for c in mv["column_configs"]:
                # Annotation lives on the first ``col`` declaration in
                # ``column_configs`` above; mypy treats the redeclaration
                # at module scope as a no-redef even though the runtime
                # is fine.  Inferring from the dict literal works here.
                col = {"column_name": c["column_name"]}
                for flag in ("enable_entity_matching", "enable_format_assistance", "exclude"):
                    if c.get(flag):
                        col[flag] = True
                desc = _from_string_array(c.get("description"))
                if desc:
                    col["description"] = desc
                cols.append(col)
            entry["column_configs"] = cols
        mvs.append(entry)
    _write_yaml(
        output_dir / "data_sources" / "metric_views.yml",
        {"metric_views": mvs},
        header="# Metric view definitions (exported)",
    )

    # -- config/sample_questions.yml -------------------------------------------
    config = ss.get("config", {})
    sq_raw = config.get("sample_questions", [])
    questions = [{"question": _from_string_array(q.get("question"))} for q in sq_raw]
    (output_dir / "config").mkdir(exist_ok=True)
    _write_yaml(
        output_dir / "config" / "sample_questions.yml", {"questions": questions}, header="# Sample questions (exported)"
    )

    # -- instructions/ ---------------------------------------------------------
    instr = ss.get("instructions", {})
    instr_dir = output_dir / "instructions"
    instr_dir.mkdir(exist_ok=True)

    # text_instructions
    text_instrs = instr.get("text_instructions", [])
    if text_instrs:
        text = _from_string_array(text_instrs[0].get("content"))
        (instr_dir / "text_instructions.md").write_text("## General Instructions\n\n" + text + "\n", encoding="utf-8")
    else:
        (instr_dir / "text_instructions.md").write_text("## General Instructions\n\n", encoding="utf-8")

    # sql_examples
    sql_dir = instr_dir / "sql_examples"
    sql_dir.mkdir(exist_ok=True)
    example_sqls = instr.get("example_question_sqls", [])
    for i, ex in enumerate(example_sqls, 1):
        question = _from_string_array(ex.get("question"))
        sql = _from_string_array(ex.get("sql"))
        filename = f"{i:02d}_{_sanitize_filename(question)}.sql"

        frontmatter: dict[str, Any] = {"question": question}
        usage = _from_string_array(ex.get("usage_guidance"))
        if usage:
            frontmatter["usage_guidance"] = usage
        if "parameters" in ex:
            params = []
            for p in ex["parameters"]:
                param: dict[str, Any] = {"name": p["name"]}
                if "type_hint" in p:
                    param["type_hint"] = p["type_hint"]
                desc = _from_string_array(p.get("description"))
                if desc:
                    param["description"] = desc
                if "default_value" in p:
                    param["default_value"] = p["default_value"].get("values", [])
                params.append(param)
            frontmatter["parameters"] = params

        fm_yaml = yaml.dump(frontmatter, default_flow_style=False, sort_keys=False, allow_unicode=True)
        content = f"---\n{fm_yaml}---\n{sql}\n"
        (sql_dir / filename).write_text(content, encoding="utf-8")

    # join_specs
    join_specs_raw = instr.get("join_specs", [])
    join_specs = []
    for j in join_specs_raw:
        sql_parts = j.get("sql", [])
        if isinstance(sql_parts, str):
            sql_parts = [sql_parts]

        sql_condition = ""
        relationship_type = "many_to_one"
        for part in sql_parts:
            if part.startswith("--rt=") and part.endswith("--"):
                rt_raw = part[5:-2]
                relationship_type = rt_raw.replace("FROM_RELATIONSHIP_TYPE_", "").lower()
            else:
                sql_condition = part

        # Annotation lives on the first ``entry`` declaration above;
        # the explicit re-annotation here trips ``no-redef``.
        entry = {
            "left": {
                "identifier": j["left"]["identifier"],
                "alias": j["left"].get("alias", ""),
            },
            "right": {
                "identifier": j["right"]["identifier"],
                "alias": j["right"].get("alias", ""),
            },
            "sql": sql_condition,
            "relationship_type": relationship_type,
        }
        comment = _from_string_array(j.get("comment"))
        if comment:
            entry["comment"] = comment
        instruction = _from_string_array(j.get("instruction"))
        if instruction:
            entry["instruction"] = instruction
        join_specs.append(entry)
    _write_yaml(instr_dir / "join_specs.yml", {"join_specs": join_specs}, header="# Join specifications (exported)")

    # sql_snippets
    snippets = instr.get("sql_snippets", {})
    snippets_out: dict[str, list[dict[str, Any]]] = {}
    for snippet_type in ("filters", "expressions", "measures"):
        items = snippets.get(snippet_type, [])
        out_items = []
        for item in items:
            # Annotation lives on the first ``entry`` declaration above.
            entry = {
                "sql": _from_string_array(item.get("sql")),
                "display_name": item.get("display_name", ""),
            }
            if "alias" in item:
                entry["alias"] = item["alias"]
            if "synonyms" in item:
                entry["synonyms"] = item["synonyms"]
            comment = _from_string_array(item.get("comment"))
            if comment:
                entry["comment"] = comment
            instruction = _from_string_array(item.get("instruction"))
            if instruction:
                entry["instruction"] = instruction
            out_items.append(entry)
        snippets_out[snippet_type] = out_items
    _write_yaml(instr_dir / "sql_snippets.yml", snippets_out, header="# SQL snippets (exported)")

    # sql_functions
    sql_funcs = instr.get("sql_functions", [])
    funcs_out = [{"identifier": f.get("identifier", "")} for f in sql_funcs]
    _write_yaml(instr_dir / "sql_functions.yml", {"sql_functions": funcs_out}, header="# SQL functions (exported)")

    # -- benchmarks ------------------------------------------------------------
    bench = ss.get("benchmarks", {})
    bench_questions = bench.get("questions", [])
    bench_out = []
    for bq in bench_questions:
        question = _from_string_array(bq.get("question"))
        answers = bq.get("answer", [])
        sql = _from_string_array(answers[0].get("content")) if answers else ""
        bench_out.append({"question": question, "sql": sql})
    (output_dir / "benchmarks").mkdir(exist_ok=True)
    _write_yaml(
        output_dir / "benchmarks" / "questions.yml", {"questions": bench_out}, header="# Benchmark questions (exported)"
    )

    print(f"Exported to {output_dir}")
    print(f"  Tables:      {len(tables)}")
    print(f"  SQL examples: {len(example_sqls)}")
    print(f"  Benchmarks:  {len(bench_out)}")


def _write_yaml(path: Path, data: Any, header: str = "") -> None:
    """Write a YAML file with an optional header comment."""
    content = yaml.dump(data, default_flow_style=False, sort_keys=False, allow_unicode=True)
    if header:
        content = header + "\n\n" + content
    path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export a Genie Space to decomposed files")
    parser.add_argument("--space-id", required=True, help="Genie Space ID to export")
    parser.add_argument("--instance", help="'<app>/<space>' to write under instances/<app>/genie_spaces/<space>")
    parser.add_argument("--output", help="Explicit output directory (overrides --instance)")
    add_project_root_arg(parser)
    args = parser.parse_args(argv)

    if args.output:
        output_dir = Path(args.output)
    elif args.instance:
        app, _, space = args.instance.partition("/")
        if not space:
            print("Error: --instance must be '<app>/<space>' so the space has a name on disk", file=sys.stderr)
            return 1
        output_dir = sub_instance_dir(args.project_root, app, "genie_spaces", space)
    else:
        print("Error: provide either --instance or --output", file=sys.stderr)
        return 1

    client = _get_client(construct=True)  # standalone CLI -- no variables available
    if client is None:
        print("Error: workspace client required to export a Genie space", file=sys.stderr)
        return 1
    print(f"Fetching space {args.space_id} ...")
    response = fetch_space(client, args.space_id)
    decompose_space(response, output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
