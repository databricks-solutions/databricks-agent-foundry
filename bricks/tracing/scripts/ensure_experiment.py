"""
Ensure MLflow Experiment (UC-Backed)
======================================
Creates (or reuses) an MLflow experiment with Unity Catalog trace
location binding.  This is an **upsert** — safe to run repeatedly.

Tracing configuration (``tracing_experiment``, ``tracing_destination``,
``tracing_table_prefix``, ``tracing_warehouse_id``) is read from
``variables`` — computed by :class:`~agent_foundry.config.InstanceConfig`
from ``foundry.toml`` and the target config.

Also configures the SQL warehouse ID required for production monitoring
scorers to query UC trace tables.

Called by ``deploy_all.py`` early in the deployment pipeline (before
Agent App) so the experiment exists when the app and dashboard deploy
try to resolve it.

Usage::

    python bricks/tracing/scripts/ensure_experiment.py \\
        --instance <your-app> --target dev
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

from databricks.sdk.errors import NotFound
from databricks.sdk.errors.base import DatabricksError

from agent_foundry.bricks import fill_state_outputs, load_brick_variables
from agent_foundry.config import TAG_KEY_MANAGED_BY
from agent_foundry.variables import add_project_root_arg
from agent_foundry.workspace_client import export_mlflow_auth_env

# Tag keys MLflow writes when binding an experiment to a UnityCatalog
# trace location.  We read these instead of deriving table names from
# variables so destroy hits exactly what MLflow created, even if
# tracing_destination / table_prefix drifted between deploys.
_MLFLOW_TRACE_TABLE_TAG_KEYS: tuple[str, ...] = (
    "mlflow.experiment.databricksTraceLogStorageTable",
    "mlflow.experiment.databricksTraceSpanStorageTable",
    "mlflow.experiment.databricksTraceAnnotationStorageTable",
    "mlflow.experiment.databricksTraceStorageTable",
)

# Tag holding the trace destination as ``catalog.schema.table_prefix``.
# MLflow creates six ``<prefix>_*`` tables but only tags four of them (the
# ``otel_metrics`` and ``trace_metadata`` tables are untagged), so we use
# this to enumerate the schema by prefix and drop *every* trace table.
_MLFLOW_TRACE_DESTINATION_TAG_KEY = "mlflow.experiment.databricksTraceDestinationPath"


def _trace_tables_from_experiment_tags(tags: dict[str, str]) -> list[str]:
    """Extract fully-qualified trace table names from experiment tags.

    Returns table names in the form ``catalog.schema.table``.  Skips
    missing / empty entries.  Order matches ``_MLFLOW_TRACE_TABLE_TAG_KEYS``
    so output is stable for tests/logging.
    """
    return [tags[k] for k in _MLFLOW_TRACE_TABLE_TAG_KEYS if tags.get(k)]


def _schema_for_trace_tables(table_fqns: list[str]) -> str:
    """Return ``catalog.schema`` if every table shares the same schema.

    Returns empty string when the table list is empty or tables span
    multiple schemas (we won't blindly drop a schema we're not sure
    about).
    """
    schemas: set[str] = set()
    for fqn in table_fqns:
        parts = fqn.split(".")
        if len(parts) >= 3:
            schemas.add(f"{parts[0]}.{parts[1]}")
    if len(schemas) == 1:
        return next(iter(schemas))
    return ""


def _trace_destination(experiment_tags: dict[str, str], variables: dict[str, str]) -> tuple[str, str]:
    """Return ``("catalog.schema", table_prefix)`` for the trace tables.

    Prefers the ``databricksTraceDestinationPath`` experiment tag
    (``catalog.schema.prefix``) since it reflects what MLflow actually
    used, and falls back to the ``tracing_destination`` /
    ``tracing_table_prefix`` deploy variables.  Returns ``("", "")`` when
    the destination can't be determined (skip prefix enumeration).
    """
    dest_path = experiment_tags.get(_MLFLOW_TRACE_DESTINATION_TAG_KEY, "")
    if dest_path.count(".") == 2:
        catalog, schema, prefix = dest_path.split(".")
        return f"{catalog}.{schema}", prefix
    destination = variables.get("tracing_destination", "")
    prefix = variables.get("tracing_table_prefix", "") or "mlflow_experiment"
    if destination.count(".") == 1:
        return destination, prefix
    return "", ""


def _prefixed_tables_in_schema(client: Any, schema_fqn: str, prefix: str) -> list[str]:
    """List UC tables in ``schema_fqn`` whose name starts with ``prefix``.

    Catches the trace tables MLflow creates but never records as
    experiment tags (``<prefix>_otel_metrics``, ``<prefix>_trace_metadata``).
    Best-effort: any listing failure returns ``[]`` so the caller falls
    back to the tagged set rather than aborting the destroy.
    """
    if not schema_fqn or not prefix or "." not in schema_fqn:
        return []
    catalog, schema = schema_fqn.split(".", 1)
    try:
        tables = client.tables.list(catalog_name=catalog, schema_name=schema)
    except Exception as exc:
        print(f"  WARNING: could not list tables in {schema_fqn} for trace cleanup: {exc}")
        return []
    return [
        f"{catalog}.{schema}.{name}" for name in (getattr(t, "name", "") for t in tables) if name.startswith(prefix)
    ]


def _sql_statement_state(response: Any) -> tuple[str, str]:
    """Extract ``(state, error_message)`` from a SQL statements API response.

    Returns ``("", "")`` when the response carries no status (older
    clients / stubs), which callers treat as success.
    """
    if not isinstance(response, dict):
        return "", ""
    status = response.get("status") or {}
    state = str(status.get("state", "")).upper()
    error = status.get("error") or {}
    message = error.get("message", "") if isinstance(error, dict) else str(error)
    return state, message


def _execute_sql(client: Any, warehouse_id: str, sql: str) -> None:
    """Run ``sql`` on ``warehouse_id`` and raise if the statement failed.

    ``api_client.do`` only raises on HTTP errors -- a statement that the
    warehouse rejects (e.g. ``DROP SCHEMA ... RESTRICT`` on a non-empty
    schema) returns HTTP 200 with ``status.state == "FAILED"``.  Without
    this check the caller reported "Dropped empty trace schema" for a
    drop that silently failed (live teardown bug).
    """
    response = client.api_client.do(
        "POST",
        "/api/2.0/sql/statements",
        body={
            "warehouse_id": warehouse_id,
            "statement": sql,
            "wait_timeout": "50s",
        },
    )
    state, message = _sql_statement_state(response)
    if state in ("FAILED", "CANCELED", "CLOSED"):
        raise RuntimeError(f"SQL statement {state}: {message or sql}")


def _destroy_trace_tables(
    variables: dict[str, str],
    *,
    experiment_tags: dict[str, str],
    ws: Any | None = None,
) -> None:
    """Drop the UC trace tables MLflow created for this experiment, then
    drop the containing schema if it became empty.

    Uses experiment tags as the source of truth for table FQNs (rather
    than deriving from ``tracing_destination``) so we drop exactly what
    MLflow created, even after config drift, then augments that with a
    prefix scan of the destination schema to catch the trace tables
    MLflow leaves untagged (``otel_metrics`` / ``trace_metadata``).

    Best-effort: every step is wrapped in a WARNING; failures here must
    not fail the destroy run because the experiment itself was already
    deleted by this point.
    """
    table_fqns = list(_trace_tables_from_experiment_tags(experiment_tags))
    schema_fqn, prefix = _trace_destination(experiment_tags, variables)
    if not table_fqns and not schema_fqn:
        return

    from agent_foundry.destroy_base import pick_any_running_warehouse

    client = ws
    if client is None:
        return

    for fqn in _prefixed_tables_in_schema(client, schema_fqn, prefix):
        if fqn not in table_fqns:
            table_fqns.append(fqn)
    if not table_fqns:
        return

    warehouse_id = variables.get("tracing_warehouse_id", "") or variables.get("warehouse_id", "")
    if not warehouse_id:
        warehouse_id = pick_any_running_warehouse(client) or ""
        if not warehouse_id:
            print(
                f"  WARNING: no warehouse_id available to drop trace tables ({len(table_fqns)} table(s) left behind)."
            )
            return
        print(f"  No dedicated warehouse_id; using running warehouse {warehouse_id} for trace cleanup.")

    for fqn in table_fqns:
        parts = fqn.split(".")
        if len(parts) < 3:
            continue
        table = f"`{parts[0]}`.`{parts[1]}`.`{parts[2]}`"
        try:
            _execute_sql(client, warehouse_id, f"DROP TABLE IF EXISTS {table}")
            print(f"  Dropped trace table {fqn}")
        except Exception as exc:
            print(f"  WARNING: Could not drop {fqn}: {exc}")

    schema_fqn = _schema_for_trace_tables(table_fqns)
    if not schema_fqn:
        return
    catalog, schema = schema_fqn.split(".", 1)
    schema_ref = f"`{catalog}`.`{schema}`"
    try:
        _execute_sql(client, warehouse_id, f"DROP SCHEMA IF EXISTS {schema_ref} RESTRICT")
        print(f"  Dropped empty trace schema {schema_fqn}")
    except Exception as exc:
        low = str(exc).lower()
        if any(marker in low for marker in ("not empty", "non-empty", "schema_not_empty", "restrict", "cascade")):
            print(f"  Trace schema {schema_fqn} not empty -- preserved (user-owned tables present).")
        else:
            print(f"  WARNING: Could not drop schema {schema_fqn}: {exc}")


def _bind_mlflow_to_databricks() -> None:
    """Point MLflow at the Databricks tracking server.

    Without this, ``mlflow.get_experiment_by_name`` / ``get_experiment``
    silently query the local default store (``mlruns/`` or sqlite) and
    return ``None`` for workspace-side experiments -- a particularly
    nasty failure mode because there is no exception, just a wrong
    answer.  Mirrors the call already made at deploy time in
    ``ensure_experiment``.
    """
    import mlflow

    mlflow.set_tracking_uri("databricks")


def _is_resource_does_not_exist(exc: BaseException) -> bool:
    """True iff the MLflow REST exception indicates the resource is gone.

    Prefer ``RestException.error_code`` when available (precise) and
    fall back to a tight substring match for older clients.  We
    deliberately do NOT match on generic phrases like ``"not found"``
    -- those are too easy to hit on transient unrelated errors and
    previously caused destroy to wipe state for still-existing
    experiments (#agent-foundry post-#258 follow-up).
    """
    error_code = getattr(exc, "error_code", None)
    if error_code == "RESOURCE_DOES_NOT_EXIST":
        return True
    msg = str(exc)
    return "RESOURCE_DOES_NOT_EXIST" in msg


def _persist_experiment_state(
    ws: Any,
    instance_dir: Path,
    target: str,
    variables: dict[str, str],
) -> None:
    """Persist experiment_id to the workspace-backed state store (#338).

    The workspace is the single source of truth (see
    :mod:`agent_foundry.state`); we no longer write a local
    ``<instance>/agent/_state`` cache, so other users and CI see the
    same state.  No-op when no experiment_id has been resolved yet.
    """
    exp_id = variables.get("experiment_id")
    if not exp_id:
        return
    from agent_foundry.state import save_state

    instance_name = instance_dir.name
    save_state(
        ws,
        instance_name,
        "tracing",
        instance_name,
        target,
        {"experiment_id": exp_id},
        action="update",
        resource_id=exp_id,
    )


def _ensure_trace_schema(ws: Any, catalog_name: str, schema_name: str) -> None:
    """Create the trace schema when missing; MLflow creates the trace tables, not the schema."""
    try:
        ws.schemas.get(full_name=f"{catalog_name}.{schema_name}")
        return
    except NotFound:
        pass
    ws.schemas.create(name=schema_name, catalog_name=catalog_name)
    print(f"  Created trace schema {catalog_name}.{schema_name}")


def ensure_experiment(
    instance_dir: Path,
    target: str,
    variables: dict[str, str],
    dry_run: bool = False,
    *,
    ws: Any | None = None,
) -> int:
    """Create or reuse the MLflow experiment with UC trace location.

    Returns 0 on success, 1 on failure.
    """
    experiment_name = variables.get("tracing_experiment", "")
    destination = variables.get("tracing_destination", "")
    table_prefix = variables.get("tracing_table_prefix", "") or "mlflow_experiment"
    warehouse_id = variables.get("tracing_warehouse_id", "") or variables.get("warehouse_id", "")

    if not experiment_name:
        print("  No tracing_experiment configured — skipping experiment creation.")
        return 0

    if not destination:
        print("  No tracing_destination configured — skipping UC binding.")
        return 0

    parts = destination.split(".")
    if len(parts) != 2:
        print(f"  ERROR: tracing_destination must be 'catalog.schema', got: {destination}")
        return 1

    catalog_name, schema_name = parts

    if dry_run:
        print(f"  [DRY RUN] Would create experiment '{experiment_name}'")
        print(f"            UC location: {catalog_name}.{schema_name}")
        print(f"            Table prefix: {table_prefix}")
        print(f"            Warehouse ID: {warehouse_id or '(not set)'}")
        return 0

    from agent_foundry import env

    if warehouse_id:
        os.environ[env.MLFLOW_TRACING_SQL_WAREHOUSE_ID.name] = warehouse_id

    export_mlflow_auth_env(variables)

    if ws is None:
        print("  ERROR: workspace client required to create the experiment")
        return 1
    try:
        _ensure_trace_schema(ws, catalog_name, schema_name)
    except DatabricksError as exc:
        print(f"  ERROR: could not create trace schema {destination}: {exc}")
        return 1
    parent_dir = experiment_name.rsplit("/", 1)[0]
    if parent_dir:
        try:
            ws.workspace.mkdirs(parent_dir)
        except Exception as exc:
            print(f"  WARNING: Could not create experiment parent directory '{parent_dir}': {exc}")

    import mlflow

    mlflow.set_tracking_uri("databricks")

    try:
        from mlflow.entities.trace_location import UnityCatalog

        uc_kwargs: dict[str, str] = {
            "catalog_name": catalog_name,
            "schema_name": schema_name,
        }
        if table_prefix:
            uc_kwargs["table_prefix"] = table_prefix

        try:
            experiment = mlflow.set_experiment(
                experiment_name=experiment_name,
                trace_location=UnityCatalog(**uc_kwargs),
            )
        except Exception as exc:
            exc_msg = str(exc)
            if "already linked" in exc_msg:
                experiment = mlflow.set_experiment(experiment_name)
                print("  Experiment already linked to UC (reusing existing binding).")
            elif "ENDPOINT_NOT_FOUND" in exc_msg or "unavailable in your workspace" in exc_msg:
                print("  WARNING: UC trace location binding unavailable on this workspace.")
                print("  Falling back to experiment without UC binding.")
                experiment = mlflow.set_experiment(experiment_name)
            else:
                raise

        variables["experiment_id"] = str(experiment.experiment_id)
        print(f"  Experiment '{experiment_name}' ready (id={experiment.experiment_id})")
        print(f"  UC trace location: {catalog_name}.{schema_name} (prefix={table_prefix or 'default'})")
    except ImportError:
        print("  WARNING: mlflow.entities.trace_location.UnityCatalog not available.")
        print("  Upgrade to mlflow >= 3.11.0 for UC-backed experiment creation.")
        experiment = mlflow.set_experiment(experiment_name)
        variables["experiment_id"] = str(experiment.experiment_id)
        print(f"  Experiment '{experiment_name}' set (id={experiment.experiment_id}, without UC binding).")

    app_name = variables.get("app_name", "")
    if app_name:
        try:
            mlflow.set_experiment_tag(TAG_KEY_MANAGED_BY, app_name)
            print(f"  Tag '{TAG_KEY_MANAGED_BY}' set to '{app_name}'")
        except Exception as exc:
            print(f"  WARNING: Could not set experiment tag: {exc}")

    _persist_experiment_state(ws, instance_dir, target, variables)

    if warehouse_id:
        try:
            from mlflow.tracing import set_databricks_monitoring_sql_warehouse_id

            set_databricks_monitoring_sql_warehouse_id(sql_warehouse_id=warehouse_id)
            print(f"  Monitoring warehouse ID set: {warehouse_id}")
        except Exception as exc:
            print(f"  WARNING: Could not set monitoring warehouse ID: {exc}")

    return 0


def _load_experiment_state(ws: Any, instance_dir: Path, target: str) -> dict[str, Any]:
    """Read tracing state from the workspace-backed store (#338).

    Returns an empty dict when ``ws`` is ``None`` (dry-run / offline) or
    when no state has been persisted yet -- callers fall back to the
    by-name + tag-gated recovery in that case.
    """
    from agent_foundry.state import load_state

    instance_name = instance_dir.name
    return load_state(ws, instance_name, "tracing", instance_name, target)


def _discover_experiment_by_name(
    experiment_name: str,
    expected_managed_by: str,
) -> str | None:
    """Look up an experiment id by name when local state is missing.

    Safety: only returns an id when the experiment carries the
    ``ai_solution_managed_by`` tag with the *expected* app name.  This
    prevents the destroy from picking up an unrelated experiment that
    happens to share a name (e.g. across solutions or environments).

    Returns ``None`` when the experiment is missing, already deleted,
    untagged, or tagged to a different solution.
    """
    if not experiment_name:
        return None
    try:
        import mlflow

        _bind_mlflow_to_databricks()
        exp = mlflow.get_experiment_by_name(experiment_name)
    except Exception:
        return None
    if exp is None:
        return None
    if getattr(exp, "lifecycle_stage", "active") == "deleted":
        return None
    tags = dict(getattr(exp, "tags", {}) or {})
    managed_by = tags.get(TAG_KEY_MANAGED_BY, "")
    if not expected_managed_by:
        return None
    if managed_by != expected_managed_by:
        print(
            f"  WARNING: experiment '{experiment_name}' exists but "
            f"{TAG_KEY_MANAGED_BY}={managed_by!r} != expected {expected_managed_by!r} "
            "-- refusing to delete (not ours)."
        )
        return None
    return str(exp.experiment_id)


def resolve_experiment_id(
    instance_dir: Path,
    target: str,
    variables: dict[str, str],
    *,
    ws: Any | None = None,
    dry_run: bool = False,
) -> str:
    """Experiment id ``destroy`` targets, or ``""`` when there is nothing to destroy.

    Resolution order:

    1. Workspace-backed state (``brick_type="tracing"``), written at
       deploy time.
    2. ``variables["experiment_id"]`` (in-process override).
    3. By-name lookup via ``mlflow.get_experiment_by_name``, gated on a
       matching ``ai_solution_managed_by`` tag so we never delete an
       experiment that wasn't deployed by *this* solution.  Skipped on
       dry-run.
    """
    state = _load_experiment_state(ws, instance_dir, target)
    experiment_id: str = state.get("experiment_id") or variables.get("experiment_id", "")
    if experiment_id:
        return experiment_id

    experiment_name = variables.get("tracing_experiment", "")
    if dry_run:
        print("  No experiment_id in state.")
        if experiment_name:
            print(f"  [DRY RUN] Would look up experiment by name '{experiment_name}'")
        return ""
    export_mlflow_auth_env(variables)
    experiment_id = _discover_experiment_by_name(experiment_name, variables.get("app_name", "")) or ""
    if not experiment_id:
        print("  No experiment_id in state and no managed experiment found by name. Nothing to destroy.")
        return ""
    print(f"  Recovered experiment id {experiment_id} by name '{experiment_name}' (tag match).")
    return experiment_id


def destroy_experiment(
    instance_dir: Path,
    target: str,
    variables: dict[str, str],
    *,
    ws: Any | None = None,
    dry_run: bool = False,
    drop_trace_tables: bool = False,
    experiment_id: str = "",
) -> int:
    """Delete the MLflow experiment and optionally UC trace tables.

    *experiment_id* defaults to :func:`resolve_experiment_id`.

    Delete pattern (from the MLflow docs / issue #12760): check
    ``lifecycle_stage`` first and only call ``delete_experiment`` when
    the experiment is still active.  Already-deleted experiments are
    treated as success without touching the API.
    """
    from agent_foundry.state import delete_state

    instance_name = instance_dir.name

    # Pin MLflow's own SDK client to the resolved profile/host before any
    # ``mlflow.*`` call below (by-name discovery *and* get/delete), else it
    # falls back to ambiguous ambient auth and 401s on shared-host profiles.
    if not dry_run:
        export_mlflow_auth_env(variables)

    experiment_id = experiment_id or resolve_experiment_id(instance_dir, target, variables, ws=ws, dry_run=dry_run)
    if not experiment_id:
        return 0

    if dry_run:
        print(f"  [DRY RUN] Would delete MLflow experiment {experiment_id}")
        if drop_trace_tables:
            print("  [DRY RUN] Would drop UC trace tables + empty trace schema (per experiment tags)")
        return 0

    import mlflow

    _bind_mlflow_to_databricks()

    try:
        exp = mlflow.get_experiment(experiment_id)
    except Exception as exc:
        if _is_resource_does_not_exist(exc):
            print(f"  MLflow experiment {experiment_id} already gone -- treating as success.")
            delete_state(ws, instance_name, "tracing", instance_name, target)
            return 0
        print(f"  WARNING: Could not look up experiment {experiment_id}: {exc}")
        return 1

    experiment_tags = dict(getattr(exp, "tags", {}) or {})

    if getattr(exp, "lifecycle_stage", "active") == "deleted":
        print(f"  MLflow experiment {experiment_id} already in 'deleted' lifecycle -- treating as success.")
        delete_state(ws, instance_name, "tracing", instance_name, target)
        if drop_trace_tables:
            _destroy_trace_tables(variables, experiment_tags=experiment_tags, ws=ws)
        return 0

    try:
        mlflow.delete_experiment(experiment_id)
        print(f"  Deleted MLflow experiment {experiment_id}")
    except Exception as exc:
        if _is_resource_does_not_exist(exc):
            print(f"  MLflow experiment {experiment_id} already gone -- treating as success.")
            delete_state(ws, instance_name, "tracing", instance_name, target)
            if drop_trace_tables:
                _destroy_trace_tables(variables, experiment_tags=experiment_tags, ws=ws)
            return 0
        print(f"  WARNING: Could not delete experiment {experiment_id}: {exc}")
        return 1

    delete_state(ws, instance_name, "tracing", instance_name, target)

    if drop_trace_tables:
        _destroy_trace_tables(variables, experiment_tags=experiment_tags, ws=ws)

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ensure MLflow experiment exists with UC trace location")
    parser.add_argument("--instance", required=True, help="Instance name")
    parser.add_argument("--target", default="dev", help="Deployment target")
    parser.add_argument("--dry-run", action="store_true", help="Preview only")
    parser.add_argument("--vars", nargs="*", default=[], help="Extra key=value vars")
    add_project_root_arg(parser)
    args = parser.parse_args(argv)

    project_root = args.project_root
    instance_dir = project_root / "instances" / args.instance
    if not instance_dir.exists():
        print(f"Error: Instance not found: {instance_dir}", file=sys.stderr)
        return 1

    variables = load_brick_variables(project_root, args.target, instance_dir, extra_vars=args.vars)
    from agent_foundry.workspace_client import prefer_workspace_client

    ws = prefer_workspace_client(variables)
    variables = fill_state_outputs(variables, instance_dir, args.target, ws)
    return ensure_experiment(instance_dir, args.target, variables, dry_run=args.dry_run, ws=ws)


if __name__ == "__main__":
    raise SystemExit(main())
