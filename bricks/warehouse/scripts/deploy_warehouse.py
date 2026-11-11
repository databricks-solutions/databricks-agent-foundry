"""
SQL Warehouse Deploy Script
==============================
Manages a dedicated SQL warehouse per application instance with custom
billing tags for cost attribution.

Operations:
  - create:  Provision a new serverless SQL warehouse with tags
  - update:  Ensure tags and settings are in sync
  - destroy: Delete the warehouse

State management:
    After a successful create, the warehouse_id is written to the shared
    workspace state (agent_foundry.state, brick type ``warehouse``).
    On subsequent deploys, the script detects the existing ID and
    uses update instead of create.

Usage:
    python bricks/warehouse/scripts/deploy_warehouse.py \
        --instance <name> [--target dev] [--action create|update|destroy] [--dry-run]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, cast

import yaml

from agent_foundry import state as shared_state
from agent_foundry.bricks import fill_state_outputs, load_brick_variables
from agent_foundry.config import merge_managed_by_tags
from agent_foundry.variables import add_project_root_arg, substitute_obj

CONFIG_FILENAME = "config.yml"


# ---------------------------------------------------------------------------
# State management (shared workspace state; flat layout keyed by the instance)
# ---------------------------------------------------------------------------

BRICK_TYPE = "warehouse"


def load_state(ws: Any | None, instance_dir: Path, target: str) -> dict[str, Any]:
    name = instance_dir.name
    return shared_state.load_state(ws, name, BRICK_TYPE, name, target)


def save_state(
    ws: Any | None,
    instance_dir: Path,
    target: str,
    state: dict[str, Any],
    *,
    action: str,
    deployer: str,
) -> None:
    name = instance_dir.name
    shared_state.save_state(
        ws,
        name,
        BRICK_TYPE,
        name,
        target,
        state,
        action=action,
        deployer=deployer,
        resource_id=state.get("warehouse_id", ""),
    )


def _delete_state(ws: Any | None, instance_dir: Path, target: str) -> None:
    name = instance_dir.name
    shared_state.delete_state(ws, name, BRICK_TYPE, name, target)


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def load_config(instance_dir: Path, variables: dict[str, str]) -> dict[str, Any]:
    """Load warehouse config, applying variable substitution and defaults.

    Auto-computed fields (``name``, ``tags.ai_solution_managed_by``) are injected from
    *variables* when absent from the instance YAML, so instances only need
    to declare overrides.
    """
    config_path = instance_dir / "warehouse" / CONFIG_FILENAME
    if not config_path.exists():
        raise FileNotFoundError(f"Warehouse config not found: {config_path}")
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config = substitute_obj(raw, variables, strict=False)

    if "name" not in config:
        config["name"] = variables.get("warehouse_display_name", "")

    extra_tags = config.get("tags") if isinstance(config.get("tags"), dict) else {}
    config["tags"] = merge_managed_by_tags(variables.get("app_name"), extra_tags)

    # ``substitute_obj`` returns ``Any`` since it walks arbitrary YAML; the
    # warehouse config root is always a mapping by contract.
    return cast(dict[str, Any], config)


# ---------------------------------------------------------------------------
# SDK helpers
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


def _get_warehouse(client: Any, warehouse_id: str) -> dict[str, Any] | None:
    """Fetch warehouse by ID, return None if not found."""
    try:
        resp = client.api_client.do("GET", f"/api/2.0/sql/warehouses/{warehouse_id}")
        # ``api_client.do`` returns ``Any``; the REST shape is owned by the
        # SQL Warehouses API and consumed as a plain mapping below.
        return cast(dict[str, Any], resp)
    except Exception:
        return None


def _find_warehouse_by_name(client: Any, name: str) -> dict[str, Any] | None:
    """Find a warehouse by name (case-insensitive)."""
    try:
        resp = client.api_client.do("GET", "/api/2.0/sql/warehouses")
        for wh in resp.get("warehouses", []):
            if wh.get("name", "").lower() == name.lower():
                return cast(dict[str, Any], wh)
    except Exception:
        pass
    return None


def _build_tags(tag_dict: dict[str, str]) -> dict[str, Any]:
    """Convert a flat tag dict to the Warehouses API format."""
    return {"custom_tags": [{"key": k, "value": v} for k, v in tag_dict.items()]}


def _create_warehouse(
    client: Any,
    config: dict[str, Any],
    dry_run: bool,
) -> str | None:
    """Create a new SQL warehouse. Returns the warehouse_id."""
    name = config["name"]
    size = config.get("size", "2X-Small")
    auto_stop_mins = config.get("auto_stop_mins", 10)
    tags = config.get("tags", {})
    min_clusters = config.get("min_num_clusters", 1)
    max_clusters = config.get("max_num_clusters", 1)

    if dry_run:
        print(
            f"  [DRY RUN] Would create warehouse '{name}' "
            f"(size={size}, scaling={min_clusters}-{max_clusters}, tags={tags})"
        )
        return None

    body: dict[str, Any] = {
        "name": name,
        "cluster_size": size,
        "auto_stop_mins": auto_stop_mins,
        "warehouse_type": "PRO",
        "enable_serverless_compute": True,
        "min_num_clusters": min_clusters,
        "max_num_clusters": max_clusters,
    }
    if tags:
        body["tags"] = _build_tags(tags)

    print(f"  Creating warehouse '{name}' (size={size}, scaling={min_clusters}-{max_clusters})...")
    resp = client.api_client.do("POST", "/api/2.0/sql/warehouses", body=body)
    # ``api_client.do`` is typed as ``Any``; the warehouse id field is a
    # string by REST contract.
    warehouse_id = cast(str | None, resp.get("id"))
    print(f"  Warehouse created: {warehouse_id}")
    return warehouse_id


def _update_warehouse(
    client: Any,
    warehouse_id: str,
    config: dict[str, Any],
    dry_run: bool,
) -> None:
    """Update an existing warehouse's tags and settings.

    Scaling (``min_num_clusters`` / ``max_num_clusters``) and ``size``
    are reconciled on every deploy, matching how ``auto_stop_mins`` and
    ``tags`` are reconciled today. Note: changing ``cluster_size`` on a
    running warehouse triggers a restart.
    """
    name = config["name"]
    tags = config.get("tags", {})
    auto_stop_mins = config.get("auto_stop_mins", 10)
    size = config.get("size", "2X-Small")
    min_clusters = config.get("min_num_clusters", 1)
    max_clusters = config.get("max_num_clusters", 1)

    if dry_run:
        print(
            f"  [DRY RUN] Would update warehouse '{warehouse_id}' "
            f"(name={name}, size={size}, scaling={min_clusters}-{max_clusters}, tags={tags})"
        )
        return

    body: dict[str, Any] = {
        "name": name,
        "cluster_size": size,
        "auto_stop_mins": auto_stop_mins,
        "min_num_clusters": min_clusters,
        "max_num_clusters": max_clusters,
    }
    if tags:
        body["tags"] = _build_tags(tags)

    print(f"  Updating warehouse '{warehouse_id}' tags and settings...")
    client.api_client.do("POST", f"/api/2.0/sql/warehouses/{warehouse_id}/edit", body=body)
    print("  Warehouse updated.")


def _destroy_warehouse(client: Any, warehouse_id: str, dry_run: bool) -> None:
    if dry_run:
        print(f"  [DRY RUN] Would delete warehouse '{warehouse_id}'")
        return
    print(f"  Deleting warehouse '{warehouse_id}'...")
    client.api_client.do("DELETE", f"/api/2.0/sql/warehouses/{warehouse_id}")
    print("  Warehouse deleted.")


# ---------------------------------------------------------------------------
# Main deploy logic
# ---------------------------------------------------------------------------


def deploy(
    instance_dir: Path,
    target: str,
    variables: dict[str, str],
    *,
    action: str | None = None,
    dry_run: bool = False,
    ws: Any | None = None,
) -> int:
    """Deploy (create/update/destroy) the dedicated SQL warehouse.

    Returns 0 on success, non-zero on error.
    """
    try:
        config = load_config(instance_dir, variables)
    except FileNotFoundError:
        return 0

    state = load_state(ws, instance_dir, target)
    existing_id = state.get("warehouse_id")
    deployer = variables.get("deployer", "")

    if action == "destroy":
        if not existing_id:
            print("  No warehouse_id in state. Nothing to destroy.")
            return 0
        if not dry_run:
            client = _get_client(variables, ws=ws)
            _destroy_warehouse(client, existing_id, dry_run)
            _delete_state(ws, instance_dir, target)
        else:
            _destroy_warehouse(None, existing_id, dry_run)
        return 0

    client = ws if dry_run else _get_client(variables, ws=ws)

    if existing_id and not dry_run:
        wh = _get_warehouse(client, existing_id)
        if wh:
            _update_warehouse(client, existing_id, config, dry_run)
            return 0
        print(f"  Warehouse '{existing_id}' not found. Will re-create.")
        existing_id = None

    if not existing_id and not dry_run:
        existing = _find_warehouse_by_name(client, config["name"])
        if existing:
            existing_id = existing["id"]
            print(f"  Found existing warehouse '{config['name']}' ({existing_id})")
            _update_warehouse(client, existing_id, config, dry_run)
            adopted = {"warehouse_id": existing_id, "name": config["name"]}
            save_state(ws, instance_dir, target, adopted, action="adopt", deployer=deployer)
            return 0

    warehouse_id = _create_warehouse(
        client,
        config,
        dry_run,
    )
    if warehouse_id:
        created = {"warehouse_id": warehouse_id, "name": config["name"]}
        save_state(ws, instance_dir, target, created, action="create", deployer=deployer)

    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deploy a dedicated SQL warehouse")
    parser.add_argument("--instance", required=True, help="Instance name")
    parser.add_argument("--target", default="dev", help="Deployment target")
    parser.add_argument("--action", choices=["create", "update", "destroy"], default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--vars", nargs="*", default=[], help="Extra variables key=val")
    add_project_root_arg(parser)
    args = parser.parse_args(argv)

    project_root = args.project_root
    instance_dir = project_root / "instances" / args.instance
    if not instance_dir.exists():
        print(f"Instance directory not found: {instance_dir}", file=sys.stderr)
        return 1

    variables = load_brick_variables(
        project_root,
        target=args.target,
        instance_dir=instance_dir,
        extra_vars=args.vars,
    )

    ws = _get_client(variables, construct=True)
    variables = fill_state_outputs(variables, instance_dir, args.target, ws)
    return deploy(instance_dir, args.target, variables, action=args.action, dry_run=args.dry_run, ws=ws)


if __name__ == "__main__":
    raise SystemExit(main())
