"""
Data Layer Deploy Script
========================
Deploys Unity Catalog volumes, curated views, materialized tables, and
metadata SQL against a Databricks workspace using the Statement Execution
API via databricks-sdk.

Scans volumes/, views/, and tables/ subdirectories for SQL files, substitutes
``${var.name}`` placeholders, splits on ';' for multi-statement files, and
executes each phase in dependency order: volumes, views, then tables.

Usage:
    python bricks/data/scripts/deploy_views.py --instance <name> [--target dev] [--dry-run]
    python bricks/data/scripts/deploy_views.py --instance catalog_explorer --target staging

The instance directory is expected at: instances/<app>/data_domains/<name>/
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml

from agent_foundry import sql_statements
from agent_foundry.bricks import fill_state_outputs, load_brick_variables, resolve_sub_instances
from agent_foundry.config import TAG_KEY_MANAGED_BY
from agent_foundry.sql_text import split_sql_statements as _split_sql_statements
from agent_foundry.variables import add_project_root_arg, substitute

# ---------------------------------------------------------------------------
# Domain config
# ---------------------------------------------------------------------------


def load_domain_config(instance_dir: Path) -> dict[str, Any]:
    """Load domain.yml from the instance directory."""
    domain_file = instance_dir / "domain.yml"
    if not domain_file.exists():
        raise FileNotFoundError(f"domain.yml not found in {instance_dir}")
    with open(domain_file, encoding="utf-8") as f:
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
# Deploy orchestration
# ---------------------------------------------------------------------------


def _collect_sql_files(instance_dir: Path) -> tuple[list[Path], list[Path]]:
    """Collect data definition SQL and metadata SQL files, sorted by name.

    Volumes are returned before views and tables so source-document storage
    exists before dependent data objects are created. Files are sorted within
    each phase for deterministic deployment.
    """
    volumes_dir = instance_dir / "volumes"
    views_dir = instance_dir / "views"
    tables_dir = instance_dir / "tables"
    metadata_dir = instance_dir / "metadata"

    data_files: list[Path] = []
    if volumes_dir.exists():
        data_files.extend(sorted(volumes_dir.rglob("*.sql")))
    if views_dir.exists():
        data_files.extend(sorted(views_dir.rglob("*.sql")))
    if tables_dir.exists():
        data_files.extend(sorted(tables_dir.rglob("*.sql")))

    metadata_files = sorted(metadata_dir.rglob("*.sql")) if metadata_dir.exists() else []

    return data_files, metadata_files


def deploy(
    instance_dir: Path,
    target: str,
    variables: dict[str, str],
    dry_run: bool = False,
    *,
    ws: Any | None = None,
) -> int:
    """Deploy all SQL files for a data domain instance.

    Args:
        instance_dir: Path to the data domain instance directory
        target: Deployment target (dev, staging, prod)
        variables: Resolved variable values
        dry_run: If True, print SQL without executing.

    Returns:
        0 on success, non-zero on error.
    """
    instance_name = instance_dir.name

    domain_cfg = load_domain_config(instance_dir)
    domain = domain_cfg.get("domain", {})

    domain_catalog = domain.get("catalog", "${var.catalog}")
    domain_schema = domain.get("schema", "${var.schema}")

    variables = {**variables}
    variables["catalog"] = substitute(domain_catalog, variables) if "${" in domain_catalog else domain_catalog
    variables["schema"] = substitute(domain_schema, variables) if "${" in domain_schema else domain_schema
    variables["domain"] = domain.get("name", instance_dir.name)

    catalog = variables["catalog"]
    schema = variables["schema"]
    warehouse_id = variables.get("warehouse_id", "")

    if not warehouse_id and not dry_run:
        print("Error: warehouse_id variable is required for deployment", file=sys.stderr)
        return 1

    data_files, metadata_files = _collect_sql_files(instance_dir)
    if not data_files and not metadata_files:
        print(f"  [{instance_name}] No SQL files found -- skipping")
        return 0

    print(f"  [{instance_name}] Target: {catalog}.{schema}")
    print(f"  [{instance_name}] Data: {len(data_files)}, Metadata: {len(metadata_files)}")

    app_tags = {TAG_KEY_MANAGED_BY: variables["app_name"]} if variables.get("app_name") else {}

    if dry_run:
        print(f"\n  [DRY RUN] Would create schema {catalog}.{schema}")
        if app_tags:
            print(f"  [DRY RUN] Would tag schema {catalog}.{schema}: {app_tags}")
        for f in data_files:
            sql = substitute(f.read_text(encoding="utf-8"), variables)
            print(f"\n  [DRY RUN] {f.name}:")
            for line in sql.strip().split("\n")[:5]:
                print(f"    {line}")
            if sql.strip().count("\n") > 4:
                print(f"    ... ({sql.strip().count(chr(10)) + 1} lines total)")
        for f in metadata_files:
            statements = _split_sql_statements(f.read_text(encoding="utf-8"), variables)
            print(f"\n  [DRY RUN] {f.name}: {len(statements)} statements")
        return 0

    client = _get_client(variables, ws=ws)

    sql_statements.ensure_schema(client, warehouse_id, catalog, schema)
    sql_statements.tag_schema(client, warehouse_id, catalog, schema, app_tags)

    errors: list[str] = []
    warnings: list[str] = []

    for f in data_files:
        print(f"  [{instance_name}] Deploying {f.name} ...")
        try:
            statements = _split_sql_statements(f.read_text(encoding="utf-8"), variables)
            for stmt in statements:
                sql_statements.execute_sql(client, warehouse_id, stmt)
            print(f"  [{instance_name}] {f.name} OK ({len(statements)} statements)")
        except Exception as exc:
            print(f"  [{instance_name}] {f.name} FAILED: {exc}", file=sys.stderr)
            errors.append(str(exc))

    for f in metadata_files:
        print(f"  [{instance_name}] Applying {f.name} ...")
        try:
            statements = _split_sql_statements(f.read_text(encoding="utf-8"), variables)
            for stmt in statements:
                sql_statements.execute_sql(client, warehouse_id, stmt)
            print(f"  [{instance_name}] {f.name} OK ({len(statements)} statements)")
        except Exception as exc:
            print(f"  [{instance_name}] {f.name} WARNING: {exc}", file=sys.stderr)
            warnings.append(str(exc))

    if errors:
        print(f"\n  [{instance_name}] Completed with {len(errors)} error(s)")
        return 1

    if warnings:
        print(f"\n  [{instance_name}] Data deployed OK with {len(warnings)} metadata warning(s)")
    else:
        print(f"  [{instance_name}] All SQL deployed successfully")
    return 0


def destroy(
    instance_dir: Path,
    target: str,
    variables: dict[str, str],
    *,
    dry_run: bool = False,
    force: bool = False,
    ws: Any | None = None,
) -> int:
    """Drop the UC schema for this data domain when *force* is True."""
    if not force:
        return 0

    domain_cfg = load_domain_config(instance_dir)
    domain = domain_cfg.get("domain", {})
    domain_catalog = domain.get("catalog", "${var.catalog}")
    domain_schema = domain.get("schema", "${var.schema}")
    variables = {**variables}
    variables["catalog"] = substitute(domain_catalog, variables) if "${" in domain_catalog else domain_catalog
    variables["schema"] = substitute(domain_schema, variables) if "${" in domain_schema else domain_schema
    catalog = variables["catalog"]
    schema = variables["schema"]
    warehouse_id = variables.get("warehouse_id", "")

    if dry_run:
        print(f"  [DRY RUN] Would drop schema {catalog}.{schema} CASCADE")
        return 0

    client = _get_client(variables, ws=ws)
    if not warehouse_id:
        from agent_foundry.destroy_base import pick_any_running_warehouse

        warehouse_id = pick_any_running_warehouse(client) or ""
        if not warehouse_id:
            print("  ERROR: warehouse_id required to drop schema (no RUNNING warehouse available for fallback)")
            return 1
        print(f"  No dedicated warehouse_id in state; using running warehouse {warehouse_id} for drop.")

    sql = f"DROP SCHEMA IF EXISTS `{catalog}`.`{schema}` CASCADE"
    print(f"  Dropping schema {catalog}.{schema} ...")
    try:
        sql_statements.execute_sql(client, warehouse_id, sql)
        print(f"  Schema {catalog}.{schema} dropped.")
    except Exception as exc:
        print(f"  ERROR dropping schema {catalog}.{schema}: {exc}")
        return 1
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deploy data domain views via Statement Execution API")
    parser.add_argument(
        "--instance",
        required=True,
        help="App name (deploys every data domain), '<app>/<domain>', or a path to either",
    )
    parser.add_argument("--target", default="dev", help="Deployment target (dev, staging, prod)")
    parser.add_argument("--dry-run", action="store_true", help="Print SQL without executing")
    parser.add_argument("--vars", nargs="*", default=[], help="Extra variables as key=value pairs")
    add_project_root_arg(parser)
    args = parser.parse_args(argv)

    project_root = args.project_root
    try:
        instance_dir, domain_dirs = resolve_sub_instances(project_root, args.instance, "data_domains")
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    variables = load_brick_variables(project_root, args.target, instance_dir, extra_vars=args.vars)
    ws = _get_client(variables, construct=True)
    variables = fill_state_outputs(variables, instance_dir, args.target, ws)

    failures = 0
    for domain_dir in domain_dirs:
        if not (domain_dir / "domain.yml").exists():
            continue
        failures += deploy(domain_dir, args.target, variables, args.dry_run, ws=ws)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
