"""
SQL Function Deploy Script
============================
Deploys Unity Catalog SQL functions from ``.sql`` files against a
Databricks workspace using the Statement Execution API via databricks-sdk.

Scans the ``functions/`` subdirectory for SQL files, substitutes
``${var.*}`` placeholders, splits on ``;`` for multi-statement files,
and executes them in alphabetical order.

The target schema defaults to the auto-computed **agent schema**
(``catalog.schema_prefix_agent``).  Functions land alongside OTEL
trace tables and can be referenced by skills via ``uc_function`` tools.

Usage:
    python bricks/sql_function/scripts/deploy_functions.py \\
        --instance <name> [--target dev] [--dry-run]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

import yaml

from agent_foundry import sql_statements
from agent_foundry import state as shared_state
from agent_foundry.bricks import fill_state_outputs, load_brick_variables, resolve_sub_instances
from agent_foundry.config import TAG_KEY_MANAGED_BY
from agent_foundry.destroy_base import uc_object_exists
from agent_foundry.sql_text import split_sql_statements as _split_sql_statements
from agent_foundry.variables import add_project_root_arg, substitute

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def load_function_config(instance_dir: Path) -> dict[str, Any]:
    """Load config.yml from the instance directory (optional)."""
    config_file = instance_dir / "config.yml"
    if not config_file.exists():
        return {}
    with open(config_file, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# ---------------------------------------------------------------------------
# SQL execution via Statement Execution API
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


# ---------------------------------------------------------------------------
# Schema management
# ---------------------------------------------------------------------------


def _grant_use_schema(
    client: Any,
    warehouse_id: str,
    catalog: str,
    schema: str,
    app_name: str,
) -> None:
    """Best-effort GRANT USE SCHEMA to the App's Service Principal.

    Requires the app to already exist (so we can look up its SP client
    ID).  Skips gracefully if the app hasn't been created yet — the
    agent_app brick's ``sp_grants`` will cover it on first full deploy.
    """
    try:
        app = client.apps.get(app_name)
    except Exception as exc:
        msg = str(exc)
        # On first deploy the app simply doesn't exist yet; the
        # agent_app brick will issue the grant when it runs.  This is
        # not a warning, it is an expected ordering.  Any other failure
        # (auth, permissions, network) still logs as WARNING.
        if "does not exist" in msg.lower() or "is deleted" in msg.lower():
            print(f"  App '{app_name}' not yet deployed; deferring USE SCHEMA grant to the Agent App brick")
        else:
            print(f"  WARNING: cannot read app '{app_name}' for USE SCHEMA grant: {exc}")
        return

    sp_client_id = app.service_principal_client_id
    if not sp_client_id:
        print("  WARNING: app has no service_principal_client_id, skipping USE SCHEMA grant")
        return

    stmt = f"GRANT USE SCHEMA ON SCHEMA `{catalog}`.`{schema}` TO `{sp_client_id}`"
    try:
        sql_statements.execute_sql(client, warehouse_id, stmt)
        print(f"  Granted USE SCHEMA on {catalog}.{schema} to app SP")
    except Exception as exc:
        if "already has" in str(exc).lower():
            print(f"  USE SCHEMA on {catalog}.{schema}: already granted")
        else:
            print(f"  WARNING: USE SCHEMA grant failed: {exc}")


# ---------------------------------------------------------------------------
# COMMENT clause check
# ---------------------------------------------------------------------------

_COMMENT_RE = re.compile(r"\bCOMMENT\b", re.IGNORECASE)


def _check_comment_clause(sql_file: Path, raw_sql: str) -> list[str]:
    """Warn if a SQL file lacks COMMENT clauses."""
    if not _COMMENT_RE.search(raw_sql):
        return [
            f"  WARNING: {sql_file.name} has no COMMENT clause. "
            "Add COMMENT on the function and parameters for LLM tool discovery."
        ]
    return []


# ---------------------------------------------------------------------------
# File collection
# ---------------------------------------------------------------------------


def _collect_sql_files(instance_dir: Path) -> list[Path]:
    """Collect .sql files from the functions/ directory, sorted by name."""
    functions_dir = instance_dir / "functions"
    if not functions_dir.exists():
        return []
    return sorted(functions_dir.rglob("*.sql"))


# ---------------------------------------------------------------------------
# State (shared workspace state; nested layout ``<app>/sql_functions/<sub>``)
# ---------------------------------------------------------------------------

BRICK_TYPE = "sql_function"


def _state_key(instance_dir: Path) -> tuple[str, str]:
    """(app instance name, sub-instance name) for ``<app>/sql_functions/<sub>``."""
    return instance_dir.parent.parent.name, instance_dir.name


def load_state(ws: Any | None, instance_dir: Path, target: str) -> dict[str, Any]:
    app, sub = _state_key(instance_dir)
    return shared_state.load_state(ws, app, BRICK_TYPE, sub, target)


def _save_state(
    ws: Any | None,
    instance_dir: Path,
    target: str,
    function_names: list[str],
    *,
    deployer: str,
) -> None:
    app, sub = _state_key(instance_dir)
    shared_state.save_state(
        ws,
        app,
        BRICK_TYPE,
        sub,
        target,
        {"deployed_functions": function_names},
        action="deploy",
        deployer=deployer,
    )


def _delete_state(ws: Any | None, instance_dir: Path, target: str) -> None:
    app, sub = _state_key(instance_dir)
    shared_state.delete_state(ws, app, BRICK_TYPE, sub, target)


def _extract_function_names(
    sql_files: list[Path],
    variables: dict[str, str],
) -> list[str]:
    """Extract function names from CREATE FUNCTION statements.

    The hyphen belongs in the character class: Unity Catalog allows it in a
    catalog or schema name, and without it a name like
    ``cat.af-probe-agent.fn`` is truncated to ``cat.af``, which then poisons
    the state file and the destroy path.
    """
    pattern = re.compile(
        r"CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+([\w.`-]+)",
        re.IGNORECASE,
    )
    names: list[str] = []
    for f in sql_files:
        raw = f.read_text(encoding="utf-8")
        substituted = substitute(raw, variables, strict=False)
        for match in pattern.finditer(substituted):
            name = match.group(1).replace("`", "")
            names.append(name)
    return names


# ---------------------------------------------------------------------------
# Deploy orchestration
# ---------------------------------------------------------------------------


def _resolve_schema_parts(
    config: dict[str, Any],
    variables: dict[str, str],
) -> tuple[str, str]:
    """Resolve catalog and schema from config or variables.

    Returns (catalog, schema_name) where schema_name is the simple
    name (without catalog prefix).
    """
    agent_schema = variables.get("agent_schema", "")
    if agent_schema and "." in agent_schema:
        default_catalog, default_schema = agent_schema.split(".", 1)
    else:
        default_catalog = variables.get("catalog", "")
        default_schema = f"{variables.get('schema_prefix', '')}_agent"

    raw_catalog = config.get("catalog", default_catalog)
    raw_schema = config.get("schema", default_schema)

    catalog = substitute(raw_catalog, variables) if "${" in raw_catalog else raw_catalog
    schema = substitute(raw_schema, variables) if "${" in raw_schema else raw_schema

    if "." in schema:
        _, schema = schema.split(".", 1)

    return catalog, schema


def deploy(
    instance_dir: Path,
    target: str,
    variables: dict[str, str],
    dry_run: bool = False,
    *,
    ws: Any | None = None,
) -> int:
    """Deploy all SQL function files for this instance.

    Returns 0 on success, non-zero on error.
    """
    instance_name = instance_dir.name
    config = load_function_config(instance_dir)

    catalog, schema = _resolve_schema_parts(config, variables)
    warehouse_id = variables.get("warehouse_id", "")
    app_name = variables.get("app_name", "")

    if not warehouse_id and not dry_run:
        print(
            "Error: warehouse_id variable is required for deployment",
            file=sys.stderr,
        )
        return 1

    sql_files = _collect_sql_files(instance_dir)
    if not sql_files:
        print(f"  [{instance_name}] No SQL files in functions/ — skipping")
        return 0

    print(f"  [{instance_name}] Target schema: {catalog}.{schema}")
    print(f"  [{instance_name}] Functions: {len(sql_files)} file(s)")

    app_tags = {TAG_KEY_MANAGED_BY: app_name} if app_name else {}

    comment_warnings: list[str] = []
    for f in sql_files:
        comment_warnings.extend(_check_comment_clause(f, f.read_text(encoding="utf-8")))

    if dry_run:
        print(f"\n  [DRY RUN] Would create schema {catalog}.{schema}")
        if app_tags:
            print(f"  [DRY RUN] Would tag schema {catalog}.{schema}: {app_tags}")
        if app_name:
            print(f"  [DRY RUN] Would GRANT USE SCHEMA to app SP ({app_name})")
        for f in sql_files:
            sql = substitute(f.read_text(encoding="utf-8"), variables)
            print(f"\n  [DRY RUN] {f.name}:")
            for line in sql.strip().split("\n")[:5]:
                print(f"    {line}")
            if sql.strip().count("\n") > 4:
                print(f"    ... ({sql.strip().count(chr(10)) + 1} lines total)")
        for w in comment_warnings:
            print(w)
        return 0

    client = _get_client(variables, ws=ws)

    sql_statements.ensure_schema(client, warehouse_id, catalog, schema)
    sql_statements.tag_schema(client, warehouse_id, catalog, schema, app_tags)

    if app_name:
        _grant_use_schema(client, warehouse_id, catalog, schema, app_name)

    errors: list[str] = []
    deployed_functions: list[str] = []

    for f in sql_files:
        print(f"  [{instance_name}] Deploying {f.name} ...")
        try:
            statements = _split_sql_statements(
                f.read_text(encoding="utf-8"),
                variables,
            )
            for stmt in statements:
                sql_statements.execute_sql(client, warehouse_id, stmt)
            print(f"  [{instance_name}] {f.name} OK ({len(statements)} statement(s))")
        except Exception as exc:
            print(
                f"  [{instance_name}] {f.name} FAILED: {exc}",
                file=sys.stderr,
            )
            errors.append(str(exc))

    for w in comment_warnings:
        print(w)

    function_names = _extract_function_names(sql_files, variables)
    deployed_functions.extend(function_names)
    _save_state(ws, instance_dir, target, deployed_functions, deployer=variables.get("deployer", ""))

    if errors:
        print(f"\n  [{instance_name}] Completed with {len(errors)} error(s)")
        return 1

    print(f"  [{instance_name}] All functions deployed successfully")
    return 0


def functions_to_drop(instance_dir: Path, target: str, variables: dict[str, str], *, ws: Any | None) -> list[str]:
    """Function FQNs ``destroy`` drops: from state, else parsed from ``functions/*.sql``.

    With a workspace client, functions Unity Catalog no longer has are
    left out, so a never-deployed project needs no warehouse.
    """
    function_names: list[str] = load_state(ws, instance_dir, target).get("deployed_functions", [])
    if not function_names:
        sql_files = sorted((instance_dir / "functions").glob("*.sql")) if (instance_dir / "functions").is_dir() else []
        function_names = _extract_function_names(sql_files, variables) if sql_files else []
    if ws is None:
        return function_names
    return [fn for fn in function_names if uc_object_exists(ws.functions.get, fn)]


def destroy(
    instance_dir: Path,
    target: str,
    variables: dict[str, str],
    *,
    dry_run: bool = False,
    force: bool = False,
    ws: Any | None = None,
) -> int:
    """Drop deployed UC SQL functions when *force* is True."""
    if not force:
        return 0

    function_names = functions_to_drop(instance_dir, target, variables, ws=ws)
    if not function_names:
        print(f"  [{instance_dir.name}] No functions to drop.")
        if not dry_run:
            _delete_state(ws, instance_dir, target)
        return 0

    warehouse_id = variables.get("warehouse_id", "")
    if dry_run:
        for fn in function_names:
            print(f"  [DRY RUN] Would drop function {fn}")
        return 0

    client = _get_client(variables, ws=ws)
    if not warehouse_id:
        from agent_foundry.destroy_base import pick_any_running_warehouse

        warehouse_id = pick_any_running_warehouse(client) or ""
        if not warehouse_id:
            print("  ERROR: warehouse_id required to drop functions (no RUNNING warehouse available for fallback)")
            return 1
        print(f"  No dedicated warehouse_id in state; using running warehouse {warehouse_id} for drops.")

    for fn in function_names:
        sql = f"DROP FUNCTION IF EXISTS {fn}"
        print(f"  Dropping function {fn} ...")
        try:
            sql_statements.execute_sql(client, warehouse_id, sql)
        except Exception as exc:
            print(f"  WARNING: Could not drop {fn}: {exc}")

    _try_drop_empty_schema(client, warehouse_id, function_names)
    _delete_state(ws, instance_dir, target)
    return 0


def _try_drop_empty_schema(
    client: Any,
    warehouse_id: str,
    function_fqns: list[str],
) -> None:
    """Attempt ``DROP SCHEMA ... RESTRICT`` for the schema that hosted
    our functions.

    Best-effort and safe by design:

    * RESTRICT (not CASCADE) -- if anything else lives in the schema
      (MLflow trace tables, user-created tables, etc.), the drop fails
      and we preserve the schema with a friendly log.
    * Single-schema guard -- only drops if every function FQN shares
      the same catalog.schema; mixed sets are left alone.
    * Idempotent -- ``IF EXISTS`` swallows "schema already gone".
    """
    schemas: set[str] = set()
    for fqn in function_fqns:
        parts = fqn.split(".")
        if len(parts) >= 3:
            schemas.add(f"{parts[0]}.{parts[1]}")
    if len(schemas) != 1:
        return
    schema_fqn = next(iter(schemas))
    catalog, schema = schema_fqn.split(".", 1)
    schema_ref = f"`{catalog}`.`{schema}`"
    try:
        sql_statements.execute_sql(client, warehouse_id, f"DROP SCHEMA IF EXISTS {schema_ref} RESTRICT")
        print(f"  Dropped empty schema {schema_fqn}")
    except Exception as exc:
        msg = str(exc)
        if "is not empty" in msg.lower() or "RESTRICT" in msg:
            print(f"  Schema {schema_fqn} not empty -- preserved (other tables/functions present).")
        else:
            print(f"  WARNING: Could not drop schema {schema_fqn}: {exc}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Deploy UC SQL functions via Statement Execution API",
    )
    parser.add_argument(
        "--instance",
        required=True,
        help="App name (deploys every function group), '<app>/<group>', or a path to either",
    )
    parser.add_argument(
        "--target",
        default="dev",
        help="Deployment target (dev, staging, prod)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print SQL without executing",
    )
    parser.add_argument(
        "--vars",
        nargs="*",
        default=[],
        help="Extra variables as key=value pairs",
    )
    add_project_root_arg(parser)
    args = parser.parse_args(argv)

    project_root = args.project_root
    try:
        instance_dir, group_dirs = resolve_sub_instances(project_root, args.instance, "sql_functions")
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    variables = load_brick_variables(
        project_root,
        args.target,
        instance_dir,
        extra_vars=args.vars,
    )
    ws = _get_client(variables, construct=True)
    variables = fill_state_outputs(variables, instance_dir, args.target, ws)

    failures = 0
    for group_dir in group_dirs:
        failures += deploy(group_dir, args.target, variables, args.dry_run, ws=ws)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
