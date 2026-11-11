"""
Observability Dashboard Deploy Script
======================================
Deploys a Lakeview (AI/BI) dashboard for agent operational monitoring.

Reads the template.lvdash.json, substitutes ``${var.name}`` placeholders in
dataset queries, and creates or updates the dashboard via the Lakeview REST API.

Uses databricks-sdk WorkspaceClient for authentication (respects
DATABRICKS_HOST, DATABRICKS_TOKEN, profiles, etc.).

Usage:
    python bricks/observability/scripts/deploy_dashboard.py \\
        --instance <name> [--target dev] [--dry-run]

State management:
    After a successful create, the dashboard_id is written to the shared
    workspace state (brick type ``dashboard``).
    On subsequent deploys, the script detects the existing dashboard_id and
    uses PATCH instead of POST automatically.
"""

from __future__ import annotations

import argparse
import json
import sys
from importlib import resources as _resources
from pathlib import Path
from typing import Any

from agent_foundry.bricks import fill_state_outputs, load_brick_variables
from agent_foundry.state import delete_state as _delete_state
from agent_foundry.state import load_state as _load_state
from agent_foundry.state import save_state as _save_state
from agent_foundry.variables import add_project_root_arg, substitute


def _load_template_text() -> str:
    """Read the packaged Lakeview dashboard template (issue #81, 68-A).

    Previously this was computed as ``Path(__file__).parent.parent /
    \"dashboards\" / \"template.lvdash.json\"``, which broke when the
    framework was installed as a wheel and ``__file__`` pointed into
    ``site-packages``.  The template now loads from package data.
    """
    ref = _resources.files("bricks.observability").joinpath(
        "dashboards",
        "template.lvdash.json",
    )
    return ref.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Instance observability config
# ---------------------------------------------------------------------------


def _derive_observability_variables(
    variables: dict[str, str],
) -> None:
    """Set derived observability variables from instance config.

    The dashboard SQL templates use ``${var.schema}`` to reference the
    UC schema containing OTEL trace views.  This is the agent schema
    (``<schema_prefix>_agent``), not the global ``schema`` default.
    """
    schema_prefix = variables.get("schema_prefix", "")
    if schema_prefix:
        variables["tracing_schema"] = f"{schema_prefix}_agent"
        variables["schema"] = variables["tracing_schema"]


# ---------------------------------------------------------------------------
# Experiment resolution
# ---------------------------------------------------------------------------


def resolve_experiment_id(
    client: Any,
    tracing_experiment: str,
) -> str | None:
    """Look up the MLflow experiment_id for a given experiment name.

    Returns the experiment_id string, or None if the experiment
    does not exist yet (agent hasn't been invoked).
    """
    try:
        resp = client.api_client.do(
            "GET",
            "/api/2.0/mlflow/experiments/get-by-name",
            query={"experiment_name": tracing_experiment},
        )
        experiment_id: str | None = resp.get("experiment", {}).get("experiment_id")
        return experiment_id
    except Exception as exc:
        print(f"  WARNING: Could not resolve experiment '{tracing_experiment}': {exc}")
        return None


# ---------------------------------------------------------------------------
# Dashboard reconciliation (find existing by name)
# ---------------------------------------------------------------------------


def find_dashboard_by_name(
    client: Any,
    display_name: str,
    parent_path: str,
) -> dict[str, str] | None:
    """Search for an existing dashboard by display name in the deployer's folder.

    Returns ``{"dashboard_id": ..., "etag": ...}`` or None.
    """
    try:
        objects = client.workspace.list(parent_path)
        for obj in objects:
            if str(getattr(obj, "object_type", "")) == "ObjectType.DASHBOARD" and display_name in str(obj.path):
                resource_id = getattr(obj, "resource_id", None)
                if resource_id:
                    dash = get_dashboard(client, resource_id)
                    return {
                        "dashboard_id": dash["dashboard_id"],
                        "etag": dash.get("etag", ""),
                        "display_name": dash.get("display_name", display_name),
                    }
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Template loading
# ---------------------------------------------------------------------------


def load_template(
    variables: dict[str, str],
    template_path: Path | None = None,
    *,
    strict: bool = True,
) -> dict[str, Any]:
    """Load the dashboard template and substitute variables in queryLines.

    When *template_path* is supplied the caller wins (used by tests with
    a custom fixture on disk).  Otherwise the template is loaded from
    the ``bricks.observability`` package data.
    """
    if template_path is not None:
        if not template_path.exists():
            raise FileNotFoundError(f"Dashboard template not found: {template_path}")
        raw = template_path.read_text(encoding="utf-8")
    else:
        raw = _load_template_text()
    dashboard: dict[str, Any] = json.loads(raw)

    for dataset in dashboard.get("datasets", []):
        query_lines = dataset.get("queryLines", [])
        dataset["queryLines"] = [substitute(line, variables, strict=strict) for line in query_lines]

    return dashboard


# ---------------------------------------------------------------------------
# API operations
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


def create_dashboard(
    client: Any,
    display_name: str,
    serialized_dashboard: str,
    warehouse_id: str,
    parent_path: str,
) -> dict[str, Any]:
    """POST /api/2.0/lakeview/dashboards; creates ``parent_path`` first (the API needs it to exist)."""
    body: dict[str, Any] = {
        "display_name": display_name,
        "serialized_dashboard": serialized_dashboard,
        "warehouse_id": warehouse_id,
    }
    if parent_path:
        client.workspace.mkdirs(parent_path)
        body["parent_path"] = parent_path
    response: dict[str, Any] = client.api_client.do("POST", "/api/2.0/lakeview/dashboards", body=body)
    return response


def update_dashboard(
    client: Any,
    dashboard_id: str,
    display_name: str,
    serialized_dashboard: str,
    warehouse_id: str,
    etag: str | None = None,
) -> dict[str, Any]:
    """PATCH /api/2.0/lakeview/dashboards/{dashboard_id}"""
    body: dict[str, Any] = {
        "display_name": display_name,
        "serialized_dashboard": serialized_dashboard,
        "warehouse_id": warehouse_id,
    }
    if etag:
        body["etag"] = etag
    response: dict[str, Any] = client.api_client.do("PATCH", f"/api/2.0/lakeview/dashboards/{dashboard_id}", body=body)
    return response


def publish_dashboard(client: Any, dashboard_id: str, embed_credentials: bool = True) -> dict[str, Any]:
    """POST /api/2.0/lakeview/dashboards/{dashboard_id}/published"""
    response: dict[str, Any] = client.api_client.do(
        "POST",
        f"/api/2.0/lakeview/dashboards/{dashboard_id}/published",
        body={"embed_credentials": embed_credentials},
    )
    return response


def get_dashboard(client: Any, dashboard_id: str) -> dict[str, Any]:
    """GET /api/2.0/lakeview/dashboards/{dashboard_id}"""
    response: dict[str, Any] = client.api_client.do("GET", f"/api/2.0/lakeview/dashboards/{dashboard_id}")
    return response


def trash_dashboard(client: Any, dashboard_id: str) -> None:
    """DELETE /api/2.0/lakeview/dashboards/{dashboard_id}"""
    client.api_client.do("DELETE", f"/api/2.0/lakeview/dashboards/{dashboard_id}")


# ---------------------------------------------------------------------------
# Deploy orchestration
# ---------------------------------------------------------------------------


def deploy(
    instance_dir: Path,
    target: str,
    variables: dict[str, str],
    action: str | None = None,
    dry_run: bool = False,
    publish: bool = True,
    ws: Any | None = None,
) -> int:
    """Deploy the observability dashboard for a given instance.

    Args:
        instance_dir: Path to the application instance directory.
        target: Deployment target (dev, staging, prod).
        variables: Resolved variable values (must include catalog, schema, warehouse_id).
        action: Explicit action (create, update, destroy). Auto-detects if None.
        dry_run: If True, load and validate but don't call the API.
        publish: If True, publish the dashboard after create/update.

    Returns:
        0 on success, non-zero on error.
    """
    instance_name = instance_dir.name
    warehouse_id = variables.get("warehouse_id", "")
    deployer = variables.get("deployer", "")
    solution_root = variables.get("solution_root", "")
    parent_path = solution_root if solution_root else (f"/Users/{deployer}" if deployer else "")
    display_name = variables.get("dashboard_display_name", f"[{target}] {instance_name}-dashboard")

    if not dry_run:
        ws = _get_client(variables, ws=ws)
    state = _load_state(
        ws,
        instance_name,
        "dashboard",
        instance_name,
        target,
    )
    existing_id = state.get("dashboard_id")
    existing_etag = state.get("etag")

    if action == "destroy":
        if not existing_id:
            print(f"No dashboard_id in state for {instance_name}/{target}. Nothing to destroy.")
            return 0
        if dry_run:
            print(f"[DRY RUN] Would destroy dashboard {existing_id}")
            return 0
        trash_dashboard(ws, existing_id)
        _delete_state(
            ws,
            instance_name,
            "dashboard",
            instance_name,
            target,
        )
        print(f"Destroyed dashboard {existing_id} ({instance_name}/{target})")
        return 0

    _derive_observability_variables(variables)

    if "experiment_id" not in variables and not dry_run:
        tracing_exp = variables.get("tracing_experiment", "")
        if tracing_exp:
            client = _get_client(variables, ws=ws)
            exp_id = resolve_experiment_id(client, tracing_exp)
            if exp_id:
                variables["experiment_id"] = exp_id
                print(f"  Resolved experiment_id: {exp_id}")
            else:
                print(
                    f"  INFO: Experiment '{tracing_exp}' not found — "
                    "UC-backed tracing tables are scoped by schema, "
                    "experiment_id filter is not required."
                )

    if dry_run:
        variables.setdefault("experiment_id", "<resolved-at-deploy-time>")

    dashboard_obj = load_template(variables, strict=not dry_run)
    serialized = json.dumps(dashboard_obj)

    if action is None:
        action = "update" if existing_id else "create"

    if not warehouse_id and not dry_run:
        print("Error: warehouse_id variable is required for deployment", file=sys.stderr)
        return 1

    if dry_run:
        print(f"[DRY RUN] Would {action} dashboard for {instance_name}/{target}")
        print(f"  Display name: {display_name}")
        print(f"  Warehouse ID: {warehouse_id}")
        print(f"  Tracing experiment: {variables.get('tracing_experiment', '(not set)')}")
        print(f"  Experiment ID: {variables.get('experiment_id', '(resolve at deploy time)')}")
        print(f"  Datasets: {len(dashboard_obj.get('datasets', []))}")
        pages = dashboard_obj.get("pages", [])
        widgets = sum(len(p.get("layout", [])) for p in pages)
        print(f"  Pages: {len(pages)}, Widgets: {widgets}")
        if existing_id:
            print(f"  Existing dashboard_id: {existing_id}")
        return 0

    def _save(dashboard_id: str, etag: str, act: str) -> None:
        new_state = {
            "dashboard_id": dashboard_id,
            "etag": etag,
            "display_name": display_name,
        }
        _save_state(
            ws,
            instance_name,
            "dashboard",
            instance_name,
            target,
            new_state,
            action=act,
            deployer=deployer,
            resource_id=dashboard_id,
        )

    if action == "create":
        if parent_path:
            existing = find_dashboard_by_name(ws, display_name, parent_path)
            if existing:
                print(f"  Found existing dashboard by name: {existing['dashboard_id']}")
                _save(existing["dashboard_id"], existing.get("etag", ""), "reconcile")
                existing_id = existing["dashboard_id"]
                existing_etag = existing.get("etag", "")
                action = "update"

    if action == "create":
        print(f"Creating dashboard: {display_name} ...")
        result = create_dashboard(ws, display_name, serialized, warehouse_id, parent_path)
        dashboard_id = result.get("dashboard_id", "")
        etag = result.get("etag", "")
        if not dashboard_id:
            print(f"Error: No dashboard_id in response: {result}", file=sys.stderr)
            return 1
        _save(dashboard_id, etag, "create")
        print(f"Created dashboard {dashboard_id}")

        if publish:
            print("Publishing dashboard ...")
            publish_dashboard(ws, dashboard_id)
            print("Dashboard published.")

        return 0

    if action == "update":
        if not existing_id:
            print(f"No existing dashboard_id for {instance_name}/{target}. Use --action create.", file=sys.stderr)
            return 1
        try:
            fresh = get_dashboard(ws, existing_id)
            existing_etag = fresh.get("etag", existing_etag)
        except Exception:
            if parent_path:
                recovered = find_dashboard_by_name(ws, display_name, parent_path)
                if recovered:
                    print(f"  Dashboard {existing_id} gone, found replacement: {recovered['dashboard_id']}")
                    existing_id = recovered["dashboard_id"]
                    existing_etag = recovered.get("etag", "")
                else:
                    print(f"  Dashboard {existing_id} gone and no replacement found. Recreating...")
                    result = create_dashboard(ws, display_name, serialized, warehouse_id, parent_path)
                    dashboard_id = result.get("dashboard_id", "")
                    etag = result.get("etag", "")
                    if not dashboard_id:
                        print(f"Error: No dashboard_id in response: {result}", file=sys.stderr)
                        return 1
                    _save(dashboard_id, etag, "recreate")
                    print(f"Recreated dashboard {dashboard_id}")
                    if publish:
                        print("Publishing dashboard ...")
                        publish_dashboard(ws, dashboard_id)
                        print("Dashboard published.")
                    return 0
            else:
                print(f"Dashboard {existing_id} not found and no parent_path for reconciliation.", file=sys.stderr)
                return 1

        print(f"Updating dashboard {existing_id}: {display_name} ...")
        result = update_dashboard(ws, existing_id, display_name, serialized, warehouse_id, existing_etag)
        # ``existing_etag`` is read from the persisted state file as
        # ``Any | None``; ``or ""`` narrows the fallback to ``str`` so
        # ``_save``'s ``etag: str`` contract is satisfied.  Empty etag
        # is the same value used by the create / recreate paths above
        # when the response omits one.
        etag = result.get("etag") or existing_etag or ""
        _save(existing_id, etag, "update")
        print(f"Updated dashboard {existing_id}")

        if publish:
            print("Publishing dashboard ...")
            publish_dashboard(ws, existing_id)
            print("Dashboard published.")

        return 0

    print(f"Unknown action: {action}", file=sys.stderr)
    return 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deploy the Agent Operations Lakeview dashboard")
    parser.add_argument(
        "--instance", required=True, help="Application instance name (resolved under instances/<name>/)"
    )
    parser.add_argument("--target", default="dev", help="Deployment target (dev, staging, prod)")
    parser.add_argument(
        "--action",
        choices=["create", "update", "destroy"],
        help="Explicit action. Auto-detects create vs update if omitted.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Load and validate only, don't call the API")
    parser.add_argument("--no-publish", action="store_true", help="Skip publishing after create/update")
    parser.add_argument("--vars", nargs="*", default=[], help="Extra variables as key=value pairs")
    add_project_root_arg(parser)
    args = parser.parse_args(argv)

    project_root = args.project_root
    instance_dir = project_root / "instances" / args.instance

    if not instance_dir.exists():
        print(f"Error: Instance directory not found: {instance_dir}", file=sys.stderr)
        return 1

    variables = load_brick_variables(project_root, args.target, instance_dir, extra_vars=args.vars)
    ws = _get_client(variables, construct=True)
    variables = fill_state_outputs(variables, instance_dir, args.target, ws)

    if not variables.get("deployer") and not args.dry_run and ws is not None:
        try:
            variables["deployer"] = ws.current_user.me().user_name
            print(f"  Resolved deployer: {variables['deployer']}")
        except Exception:
            pass

    return deploy(
        instance_dir,
        args.target,
        variables,
        action=args.action,
        dry_run=args.dry_run,
        publish=not args.no_publish,
        ws=ws,
    )


if __name__ == "__main__":
    raise SystemExit(main())
