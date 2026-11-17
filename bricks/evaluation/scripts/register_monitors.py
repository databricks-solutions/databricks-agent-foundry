"""
Production Monitoring Registration
====================================
Registers and manages MLflow scorers for continuous production
monitoring.  Reads scorer configuration from ``eval_config.yml``
and uses the MLflow production monitoring API to register, start,
stop, or inspect scorer status.

The same scorers used during development evaluation are reused here,
ensuring consistent quality measurement across the lifecycle.

Usage::

    python bricks/evaluation/scripts/register_monitors.py \\
        --instance <your-app> --target dev --action start

    python bricks/evaluation/scripts/register_monitors.py \\
        --instance <your-app> --target dev --action stop

    python bricks/evaluation/scripts/register_monitors.py \\
        --instance <your-app> --target dev --action status
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from agent_foundry.bricks import load_brick_variables
from agent_foundry.variables import add_project_root_arg
from bricks.evaluation.config import get_experiment_name, load_eval_config

# ---------------------------------------------------------------------------
# Scorer resolution for production
# ---------------------------------------------------------------------------


def resolve_prod_scorers(
    config: dict[str, Any],
    instance_dir: Path | None = None,
) -> list[tuple[str, Any]]:
    """Build (name, scorer) pairs for production monitoring.

    Scorers from ``<instance_dir>/evaluation/scorers/*.py`` can be listed
    alongside built-in judges and framework scorers.

    Returns a list of tuples: (registration_name, scorer_instance).
    """
    from bricks.evaluation.scorers.builtin import (
        BUILTIN_JUDGE_NAMES,
        get_builtin_judges,
        parse_scorer_entry,
    )
    from bricks.evaluation.scorers.registry import build_custom_registry

    custom_registry = build_custom_registry(instance_dir)

    prod_config = config.get("production_monitoring", {})
    scorer_entries = prod_config.get("scorers", [])

    result: list[tuple[str, Any]] = []
    for entry in scorer_entries:
        name, args = parse_scorer_entry(entry)
        if name in BUILTIN_JUDGE_NAMES:
            judge = get_builtin_judges([entry])[0]
            # Plain entries keep their historical registration name so
            # already-registered monitors are not duplicated.
            result.append((judge.name if args else name.lower(), judge))
        elif args:
            raise ValueError(f"Custom scorer '{name}' does not take args")
        elif name in custom_registry:
            result.append((name, custom_registry[name]))
        else:
            print(f"  WARNING: Unknown scorer '{name}', skipping")

    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _init_mlflow_experiment(
    experiment_name: str,
    variables: dict[str, str] | None,
) -> None:
    """Set tracking URI and experiment with UC binding if available."""
    import os

    import mlflow

    from agent_foundry import env
    from agent_foundry.workspace_client import export_mlflow_auth_env

    vars_ = variables or {}

    # Pin the resolved auth for MLflow's *own* SDK client, then route
    # tracking through the target's CLI profile. Both are needed: a bare
    # ``"databricks"`` URI registers monitors on the default profile's
    # workspace, and the URI alone never reaches MLflow's client (#421).
    export_mlflow_auth_env(vars_)
    profile = vars_.get("profile")
    mlflow.set_tracking_uri(f"databricks://{profile}" if profile else "databricks")

    # Tracing config comes from ``variables`` (computed by
    # ``InstanceConfig`` from ``foundry.toml`` + targets).
    warehouse_id = vars_.get("tracing_warehouse_id", "")
    if warehouse_id:
        os.environ[env.MLFLOW_TRACING_SQL_WAREHOUSE_ID.name] = warehouse_id

    destination = vars_.get("tracing_destination", "")
    table_prefix = vars_.get("tracing_table_prefix", "") or "mlflow_experiment"
    if destination:
        parts = destination.split(".")
        if len(parts) == 2:
            try:
                from mlflow.entities.trace_location import UnityCatalog

                uc_kwargs: dict[str, str] = {
                    "catalog_name": parts[0],
                    "schema_name": parts[1],
                }
                if table_prefix:
                    uc_kwargs["table_prefix"] = table_prefix
                mlflow.set_experiment(
                    experiment_name=experiment_name,
                    trace_location=UnityCatalog(**uc_kwargs),
                )
                return
            except (ImportError, Exception):
                pass

    mlflow.set_experiment(experiment_name)


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


def action_start(
    config: dict[str, Any],
    instance_dir: Path | None = None,
    variables: dict[str, str] | None = None,
) -> int:
    """Register and start all production scorers."""
    from mlflow.genai.scorers import ScorerSamplingConfig

    from agent_foundry import env

    experiment_name = get_experiment_name(config, variables)
    _init_mlflow_experiment(experiment_name, variables)

    warehouse_id = config.get("warehouse_id") or env.read_optional(env.MLFLOW_TRACING_SQL_WAREHOUSE_ID) or ""
    if warehouse_id:
        try:
            from mlflow.tracing import set_databricks_monitoring_sql_warehouse_id

            set_databricks_monitoring_sql_warehouse_id(sql_warehouse_id=warehouse_id)
            print(f"  Monitoring warehouse: {warehouse_id}")
        except (ImportError, Exception) as exc:
            print(f"  WARNING: Could not set monitoring warehouse: {exc}")

    prod_config = config.get("production_monitoring", {})
    sample_rate = prod_config.get("sample_rate", 0.5)

    scorers = resolve_prod_scorers(config, instance_dir)
    if not scorers:
        print("No production scorers configured.")
        return 1

    print(f"Registering {len(scorers)} scorer(s) with sample_rate={sample_rate} ...")
    for reg_name, scorer_obj in scorers:
        try:
            registered = scorer_obj.register(name=reg_name)
            registered.start(
                sampling_config=ScorerSamplingConfig(sample_rate=sample_rate),
            )
            print(f"  Started: {reg_name} (sample_rate={sample_rate})")
        except Exception as e:
            print(f"  FAILED: {reg_name} -- {e}")

    print("Production monitoring started.")
    return 0


def action_stop(
    config: dict[str, Any],
    instance_dir: Path | None = None,
    variables: dict[str, str] | None = None,
) -> int:
    """Stop all registered production scorers."""
    from mlflow.genai.scorers import list_scorers

    experiment_name = get_experiment_name(config, variables)
    _init_mlflow_experiment(experiment_name, variables)

    scorers = list_scorers()
    if not scorers:
        print("No registered scorers found.")
        return 0

    for s in scorers:
        name = getattr(s, "_server_name", getattr(s, "name", "unknown"))
        try:
            s.stop()
            print(f"  Stopped: {name}")
        except Exception as e:
            print(f"  FAILED to stop {name}: {e}")

    print("All scorers stopped.")
    return 0


def action_status(
    config: dict[str, Any],
    instance_dir: Path | None = None,
    variables: dict[str, str] | None = None,
) -> int:
    """List all registered scorers and their status."""
    from mlflow.genai.scorers import list_scorers

    experiment_name = get_experiment_name(config, variables)
    _init_mlflow_experiment(experiment_name, variables)

    scorers = list_scorers()
    if not scorers:
        print("No registered scorers found.")
        return 0

    print(f"{'Name':<35} {'Sample Rate':<15} {'Status'}")
    print("-" * 65)
    for s in scorers:
        name = getattr(s, "_server_name", getattr(s, "name", "unknown"))
        rate = getattr(s, "sample_rate", "?")
        status = "active" if rate and float(rate) > 0 else "stopped"
        print(f"  {name:<33} {str(rate):<13} {status}")

    return 0


def action_delete(
    config: dict[str, Any],
    instance_dir: Path | None = None,
    variables: dict[str, str] | None = None,
) -> int:
    """Delete all registered scorers from the experiment."""
    from mlflow.genai.scorers import delete_scorer, list_scorers

    experiment_name = get_experiment_name(config, variables)
    _init_mlflow_experiment(experiment_name, variables)

    scorers = list_scorers()
    if not scorers:
        print("No registered scorers to delete.")
        return 0

    for s in scorers:
        name = getattr(s, "_server_name", getattr(s, "name", "unknown"))
        try:
            delete_scorer(name=name)
            print(f"  Deleted: {name}")
        except Exception as e:
            print(f"  FAILED to delete {name}: {e}")

    print("All scorers deleted.")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage MLflow production monitoring scorers")
    parser.add_argument(
        "--instance",
        required=True,
        help="Application instance name (under instances/<name>/)",
    )
    parser.add_argument(
        "--target",
        default="dev",
        help="Deployment target (dev, staging, prod)",
    )
    parser.add_argument(
        "--action",
        required=True,
        choices=["start", "stop", "status", "delete"],
        help="Action to perform",
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
    instance_dir = project_root / "instances" / args.instance

    if not instance_dir.exists():
        print(f"Error: Instance directory not found: {instance_dir}", file=sys.stderr)
        return 1

    variables = load_brick_variables(project_root, args.target, instance_dir, extra_vars=args.vars)
    config = load_eval_config(instance_dir, variables)

    print(f"Production monitoring: instance={args.instance} action={args.action}")

    actions = {
        "start": action_start,
        "stop": action_stop,
        "status": action_status,
        "delete": action_delete,
    }
    return actions[args.action](config, instance_dir=instance_dir, variables=variables)


if __name__ == "__main__":
    raise SystemExit(main())
