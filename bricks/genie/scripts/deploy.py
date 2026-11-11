"""
Genie Space Deploy Script
=========================
Wraps the Databricks Genie Management REST API. Reads the assembled
api_payload.json and performs create, update, or destroy operations.

Uses databricks-sdk WorkspaceClient for authentication (respects
DATABRICKS_HOST, DATABRICKS_TOKEN, profiles, etc.).

Usage:
    python bricks/genie/scripts/deploy.py --instance <name> [--target dev] [--action create|update|destroy] [--dry-run]

State management:
    After a successful create, the space_id is written to the shared
    workspace state (brick type ``genie_spaces``, one per sub-instance).
    On subsequent deploys, the script detects the existing space_id and
    uses UPDATE instead of CREATE automatically.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, cast

from agent_foundry.bricks import fill_state_outputs, load_brick_variables, resolve_sub_instances
from agent_foundry.state import delete_state as _delete_state
from agent_foundry.state import load_state as _load_state
from agent_foundry.state import save_state as _save_state
from agent_foundry.variables import add_project_root_arg
from bricks.genie.scripts.assemble import assemble_api_payload

# ---------------------------------------------------------------------------
# API operations
# ---------------------------------------------------------------------------


def _get_client(
    variables: dict[str, str] | None = None,
    *,
    ws: Any | None = None,
    construct: bool = False,
) -> Any | None:
    """Use the injected client. Factory only when *construct* (standalone ``main``)."""
    if ws is not None:
        return ws
    if not construct:
        return None
    from agent_foundry.workspace_client import prefer_workspace_client

    return prefer_workspace_client(variables)


def create_space(client: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """POST /api/2.0/genie/spaces; creates ``parent_path`` first (the API needs it to exist)."""
    parent_path = payload.get("parent_path", "")
    if parent_path:
        client.workspace.mkdirs(parent_path.removeprefix("/Workspace"))
    response = client.api_client.do("POST", "/api/2.0/genie/spaces", body=payload)
    # ``api_client.do`` is typed as ``Any``; the Genie spaces API contract
    # returns a mapping.
    return cast(dict[str, Any], response)


def update_space(client: Any, space_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """PATCH /api/2.0/genie/spaces/{space_id}"""
    response = client.api_client.do("PATCH", f"/api/2.0/genie/spaces/{space_id}", body=payload)
    return cast(dict[str, Any], response)


def trash_space(client: Any, space_id: str) -> None:
    """DELETE /api/2.0/genie/spaces/{space_id}"""
    client.api_client.do("DELETE", f"/api/2.0/genie/spaces/{space_id}")


# ---------------------------------------------------------------------------
# Deploy orchestration
# ---------------------------------------------------------------------------


def deploy(
    instance_dir: Path,
    target: str,
    variables: dict[str, str],
    action: str | None = None,
    dry_run: bool = False,
    *,
    ws: Any | None = None,
) -> int:
    """Run the deploy workflow for a single Genie Space instance.

    Args:
        instance_dir: Path to the genie space instance directory
        target: Deployment target (dev, staging, prod)
        variables: Resolved variable values
        action: Explicit action (create, update, destroy). Auto-detects if None.
        dry_run: If True, assemble and print but don't call the API.

    Returns:
        0 on success, non-zero on error.
    """
    sub_instance = instance_dir.name
    app_instance = instance_dir.parent.parent
    instance_name = app_instance.name
    deployer = variables.get("deployer", "")

    try:
        ws = _get_client(variables, ws=ws)
    except Exception:
        ws = None
    state = _load_state(
        ws,
        instance_name,
        "genie_spaces",
        sub_instance,
        target,
    )
    existing_space_id = state.get("space_id")

    if action == "destroy":
        if not existing_space_id:
            print(f"No space_id in state for {sub_instance}/{target}. Nothing to destroy.")
            return 0
        if dry_run:
            print(f"[DRY RUN] Would destroy space {existing_space_id}")
            return 0
        trash_space(ws, existing_space_id)
        _delete_state(
            ws,
            instance_name,
            "genie_spaces",
            sub_instance,
            target,
        )
        print(f"Destroyed space {existing_space_id} ({sub_instance}/{target})")
        return 0

    try:
        payload = assemble_api_payload(instance_dir, variables)
    except ValueError as e:
        print(f"Error assembling {sub_instance}: {e}", file=sys.stderr)
        return 1

    if action is None:
        action = "update" if existing_space_id else "create"

    if dry_run:
        print(f"[DRY RUN] Would {action} space for {sub_instance}/{target}")
        print(f"  Title: {payload['title']}")
        print(f"  Description: {payload['description']}")
        print(f"  Warehouse ID: {payload['warehouse_id']}")
        ss = json.loads(payload["serialized_space"])
        n_tables = len(ss.get("data_sources", {}).get("tables", []))
        n_examples = len(ss.get("instructions", {}).get("example_question_sqls", []))
        print(f"  Tables: {n_tables}, SQL examples: {n_examples}")
        if existing_space_id:
            print(f"  Existing space_id: {existing_space_id}")
        return 0

    def _save(space_id: str, act: str) -> None:
        new_state = {"space_id": space_id, "title": payload["title"]}
        _save_state(
            ws,
            instance_name,
            "genie_spaces",
            sub_instance,
            target,
            new_state,
            action=act,
            deployer=deployer,
            resource_id=space_id,
        )

    if action == "create":
        print(f"Creating space: {payload['title']} ...")
        try:
            result = create_space(ws, payload)
        except Exception as exc:
            print(f"Error creating space: {exc}", file=sys.stderr)
            return 1
        space_id = result.get("space_id", result.get("id", ""))
        if not space_id:
            print(f"Error: No space_id in response: {result}", file=sys.stderr)
            return 1
        _save(space_id, "create")
        print(f"Created space {space_id}")
        return 0

    if action == "update":
        if not existing_space_id:
            print(f"No existing space_id for {sub_instance}/{target}. Use --action create.", file=sys.stderr)
            return 1
        _update_exclude = {"parent_path", "title"}
        update_payload = {k: v for k, v in payload.items() if k not in _update_exclude}
        print(f"Updating space {existing_space_id}: {payload['title']} ...")
        try:
            update_space(ws, existing_space_id, update_payload)
        except Exception as exc:
            exc_str = str(exc)
            if any(s in exc_str.lower() for s in ("trashed", "not found", "permission")):
                print(f"Space {existing_space_id} is gone ({exc}). Recreating...", file=sys.stderr)
                try:
                    result = create_space(ws, payload)
                except Exception as create_exc:
                    print(f"Error recreating space: {create_exc}", file=sys.stderr)
                    return 1
                space_id = result.get("space_id", result.get("id", ""))
                if not space_id:
                    print(f"Error: No space_id in response: {result}", file=sys.stderr)
                    return 1
                _save(space_id, "recreate")
                print(f"Recreated space {space_id}")
                return 0
            print(f"Error updating space {existing_space_id}: {exc}", file=sys.stderr)
            return 1
        _save(existing_space_id, "update")
        print(f"Updated space {existing_space_id}")
        return 0

    print(f"Unknown action: {action}", file=sys.stderr)
    return 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deploy a Genie Space via the Management API")
    parser.add_argument(
        "--instance",
        required=True,
        help="App name (deploys every Genie Space), '<app>/<space>', or a path to either",
    )
    parser.add_argument("--target", default="dev", help="Deployment target (dev, staging, prod)")
    parser.add_argument(
        "--action",
        choices=["create", "update", "destroy"],
        help="Explicit action. Auto-detects create vs update if omitted.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Assemble and validate only, don't call the API")
    parser.add_argument("--vars", nargs="*", default=[], help="Extra variables as key=value pairs")
    add_project_root_arg(parser)
    args = parser.parse_args(argv)

    project_root = args.project_root
    try:
        instance_dir, space_dirs = resolve_sub_instances(project_root, args.instance, "genie_spaces")
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    variables = load_brick_variables(project_root, args.target, instance_dir, extra_vars=args.vars)
    ws = _get_client(variables, construct=True)
    variables = fill_state_outputs(variables, instance_dir, args.target, ws)

    failures = 0
    for space_dir in space_dirs:
        failures += deploy(space_dir, args.target, variables, args.action, args.dry_run, ws=ws)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
