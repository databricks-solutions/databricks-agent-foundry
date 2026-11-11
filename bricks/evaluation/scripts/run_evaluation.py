"""
Local Evaluation Runner (user identity, real OBO)
==================================================
Runs ``mlflow.genai.evaluate()`` from the developer's laptop against
the deployed Databricks App or an in-process agent.

Caller identity
---------------
When ``--predict-via deployed`` (the default), this script calls the
app's ``/invocations`` endpoint using the laptop's interactive OAuth
session (from ``databricks auth login --profile <name>``). That
session carries the app's ``.auth/callback`` cookies, so the Apps
OAuth proxy forwards the user identity via the ``gap-auth`` header
and the agent executes with real **on-behalf-of (OBO)** semantics.
OBO-dependent skills (e.g. ``my_portfolio``) resolve the real user.

Modes
-----
  - ``--mode direct``   Run the agent on a dataset, then score.
  - ``--mode traces``   Score already-logged traces; do not call the agent.

In direct mode, ``--predict-via`` picks the execution path:

  - ``deployed`` (default): HTTP to the live app, then fetch the deep
    UC OTel trace via ``mlflow.get_trace`` and pass it to
    ``mlflow.genai.evaluate(data=records_with_trace)``. Real OBO, deep
    trace, TOOL/RETRIEVER spans visible to code-based scorers.
  - ``local``: Instantiates the agent in-process. Captures AGENT /
    TOOL / RETRIEVER spans, but runs as your laptop's SDK identity
    and does NOT exercise OBO.

The deployed flow uses the ``x-mlflow-return-trace-id`` header
(MLflow 3.11+, see mlflow/mlflow#20387) to correlate the laptop
eval item with the trace the deployed app writes to UC OTel tables.

Data sources (``--dataset``)
----------------------------
  - Omit: auto-discover YAML files from instance + brick directories
  - UC EvaluationDataset name: ``catalog.schema.dataset_name``
  - Delta table: ``catalog.schema.golden_set``

Usage::

    # OBO eval against deployed app (default)
    python bricks/evaluation/scripts/run_evaluation.py \\
        --instance <your-app> --target dev

    # In-process deep-trace eval (no OBO)
    python bricks/evaluation/scripts/run_evaluation.py \\
        --instance <your-app> --target dev \\
        --predict-via local

    # Score existing production traces
    python bricks/evaluation/scripts/run_evaluation.py \\
        --instance <your-app> --target dev \\
        --mode traces --max-traces 50
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from importlib import resources as _resources
from pathlib import Path
from typing import Any

import yaml

from agent_foundry.bricks import fill_state_outputs, load_brick_variables
from agent_foundry.variables import (
    add_project_root_arg,
    substitute_obj,
)
from bricks.evaluation.config import load_eval_config

# ---------------------------------------------------------------------------
# Dataset loading
# ---------------------------------------------------------------------------


def load_dataset_from_yaml(path: Path) -> list[dict[str, Any]]:
    """Load an evaluation dataset from a YAML file.

    Expected format::

        dataset:
          - inputs: {question: "..."}
            expectations: {expected_tools: [...]}
    """
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    dataset: list[dict[str, Any]] = data.get("dataset", [])
    return dataset


def substitute_record_expectations(
    records: list[dict[str, Any]],
    variables: dict[str, str],
) -> list[dict[str, Any]]:
    """Resolve ``${var.<name>}`` placeholders inside record ``expectations``.

    Substitution is intentionally scoped to the ``expectations`` sub-tree
    so that ``inputs.question`` -- the verbatim question we want to ask
    the agent -- is preserved as-is. The primary motivation is per-target
    catalog/schema templating in ``expected_sql`` (e.g.
    ``SELECT * FROM ${var.catalog}.foo``).

    Records without an ``expectations`` key are passed through unchanged.
    Unknown variables are left in place (``strict=False``) so legacy
    datasets that happen to contain a ``${var.…}`` literal still load.
    """
    out: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, dict) or "expectations" not in record:
            out.append(record)
            continue
        new_record = dict(record)
        new_record["expectations"] = substitute_obj(
            record["expectations"],
            variables,
            strict=False,
        )
        out.append(new_record)
    return out


def find_datasets(instance_dir: Path | None) -> list[Path]:
    """Discover YAML evaluation datasets for an instance.

    Brick-packaged datasets are located via ``importlib.resources`` so
    the brick works when installed as a wheel (issue #81, 68-A).  In
    practice this still resolves to a real filesystem path for normal
    pip installs; it only changes behaviour under zipapp packaging,
    which is not a supported scenario.
    """
    paths: list[Path] = []
    if instance_dir is not None:
        ds_dir = instance_dir / "evaluation" / "datasets"
        if ds_dir.is_dir():
            paths.extend(sorted(ds_dir.glob("*.yml")))
            paths.extend(sorted(ds_dir.glob("*.yaml")))

    try:
        brick_ds_ref = _resources.files("bricks.evaluation").joinpath("datasets")
        brick_ds = Path(str(brick_ds_ref))
    except (ModuleNotFoundError, FileNotFoundError):
        brick_ds = None

    if brick_ds is not None and brick_ds.is_dir():
        for p in sorted(brick_ds.glob("*.yml")):
            if p.name.startswith("_"):
                continue
            paths.append(p)

    return paths


def load_dataset_from_uc(dataset_name: str) -> Any:
    """Load a governed MLflow EvaluationDataset from Unity Catalog.

    Routes through the tracking URI set by ``_init_mlflow_experiment``
    (``databricks://<profile>``), so no Spark session is needed.

    Args:
        dataset_name: Fully qualified UC name (``catalog.schema.name``).

    Returns:
        An MLflow ``EvaluationDataset``.

    Raises:
        RuntimeError: If the dataset isn't registered. Register a golden
        set with ``mlflow.genai.datasets.create_dataset(...)`` before
        evaluating; raw Delta tables are intentionally not supported here.
    """
    import mlflow.genai.datasets

    try:
        ds = mlflow.genai.datasets.get_dataset(dataset_name)
    except Exception as exc:
        raise RuntimeError(
            f"Could not load UC EvaluationDataset '{dataset_name}': {exc}. "
            "Check the tracking-URI profile and that the dataset is "
            "registered via mlflow.genai.datasets.create_dataset(...)."
        ) from exc
    print(f"  Loaded UC EvaluationDataset: {dataset_name}")
    return ds


# ---------------------------------------------------------------------------
# Scorer resolution
# ---------------------------------------------------------------------------


def resolve_scorers(
    config: dict[str, Any],
    has_expectations: bool = False,
    traces_mode: bool = False,
) -> list[Any]:
    """Build the scorer list from config, mixing built-in judges and custom scorers.

    Args:
        has_expectations: Include ``with_ground_truth`` scorers (Correctness, etc.).
        traces_mode: Include ``traces_only`` scorers (RetrievalGroundedness, etc.)
            that need access to trace internals (e.g. RETRIEVER spans).
    """
    from bricks.evaluation.scorers.builtin import get_builtin_judges
    from bricks.evaluation.scorers.genie_accuracy import genie_sql_correctness
    from bricks.evaluation.scorers.response_quality import response_quality
    from bricks.evaluation.scorers.routing import tool_routing_accuracy

    custom_registry = {
        "tool_routing_accuracy": tool_routing_accuracy,
        "genie_sql_correctness": genie_sql_correctness,
        "response_quality": response_quality,
    }

    scorer_config = config.get("scorers", {})
    scorers: list[Any] = []

    default_names = scorer_config.get("default", [])
    if default_names:
        scorers.extend(get_builtin_judges(default_names))

    if has_expectations:
        gt_names = scorer_config.get("with_ground_truth", [])
        if gt_names:
            scorers.extend(get_builtin_judges(gt_names))

    if traces_mode:
        trace_names = scorer_config.get("traces_only", [])
        if trace_names:
            scorers.extend(get_builtin_judges(trace_names))

    custom_names = scorer_config.get("custom", [])
    for name in custom_names:
        if name in custom_registry:
            scorers.append(custom_registry[name])
        else:
            print(f"  WARNING: Unknown custom scorer '{name}', skipping")

    return scorers


# ---------------------------------------------------------------------------
# Evaluation modes
# ---------------------------------------------------------------------------


def run_direct_evaluation(
    config: dict[str, Any],
    instance_dir: Path | None,
    variables: dict[str, str],
    dataset_name: str | None = None,
    dry_run: bool = False,
    predict_via: str = "local",
    *,
    ws: Any | None = None,
) -> int:
    """Run direct evaluation: invoke agent on dataset, then score.

    Args:
        predict_via: ``"local"`` instantiates the agent in-process so
            MLflow captures full traces (TOOL / RETRIEVER spans).
            ``"deployed"`` calls the live Databricks App HTTP endpoint
            (black-box, no internal spans).

    When *dataset_name* is provided, loads from UC (EvaluationDataset or
    Delta table).  Otherwise, discovers YAML files from the instance
    and brick directories.

    A record's ``inputs`` may carry optional ``custom_inputs`` and
    ``context`` mappings next to ``question``.  Both predict modes forward
    them as the matching ``ResponsesAgentRequest`` fields (see
    ``_request_extras``); they are not ``${var.*}``-templated.
    """
    import mlflow

    experiment_name = _get_experiment_name(config, variables)
    _init_mlflow_experiment(experiment_name, variables)
    print(f"MLflow experiment: {experiment_name}")

    eval_data: Any
    has_expectations: bool

    if dataset_name:
        eval_data = load_dataset_from_uc(dataset_name)
        has_expectations = _uc_data_has_expectations(eval_data)
    else:
        yaml_paths = find_datasets(instance_dir)
        if not yaml_paths:
            print("No evaluation datasets found. Provide --dataset or create YAML files in:")
            if instance_dir:
                print(f"  {instance_dir / 'evaluation' / 'datasets' / '*.yml'}")
            print("  bricks/evaluation/datasets/*.yml (shipped with the brick)")
            return 1

        all_records: list[dict[str, Any]] = []
        for ds_path in yaml_paths:
            records = load_dataset_from_yaml(ds_path)
            print(f"  Loaded {len(records)} records from {ds_path.name}")
            all_records.extend(records)

        if not all_records:
            print("No evaluation records found in datasets.")
            return 1

        all_records = substitute_record_expectations(all_records, variables)

        eval_data = all_records
        has_expectations = any("expectations" in r for r in all_records)

    # Both local and deployed paths now produce deep traces, so trace-only
    # scorers (RetrievalGroundedness, ...) are valid in either mode.
    scorers = resolve_scorers(
        config,
        has_expectations=has_expectations,
        traces_mode=True,
    )
    print(f"  Scorers: {len(scorers)} ({', '.join(_scorer_name(s) for s in scorers)})")

    if dry_run:
        print(f"[DRY RUN] Would run direct evaluation (predict_via={predict_via})")
        return 0

    if predict_via == "deployed":
        records = _as_records(eval_data)
        if not records:
            print("No records to evaluate.")
            return 1
        records = substitute_record_expectations(records, variables)
        enriched = _fetch_deployed_traces(records, instance_dir, variables, ws=ws)
        if not enriched:
            print("No traces could be fetched from the deployed app.")
            return 1
        print(f"Running evaluation on {len(enriched)} pre-fetched trace(s) ...")
        results = mlflow.genai.evaluate(
            data=enriched,
            scorers=scorers,
        )
    else:
        predict_fn = _build_local_predict_fn(instance_dir, variables)
        print("Running evaluation ...")
        results = mlflow.genai.evaluate(
            data=eval_data,
            predict_fn=predict_fn,
            scorers=scorers,
        )
    _print_results(results)
    return 0


def _get_experiment_name(
    config: dict[str, Any],
    variables: dict[str, str] | None = None,
) -> str:
    """Return the experiment name (single experiment for all traces and evaluation).

    Falls back to ``variables["tracing_experiment"]`` (computed by InstanceConfig)
    when the eval config doesn't specify an explicit experiment name.
    """
    name: str | None = config.get("experiment_name") or config.get("production_experiment")
    if name:
        return name
    if variables and variables.get("tracing_experiment"):
        return variables["tracing_experiment"]
    return "agent_evaluation"


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

    # The tracking URI does not reach the ``WorkspaceClient`` MLflow builds
    # for itself, which is what ``mlflow.get_trace`` uses to read UC OTel
    # tables (#421). Pin the resolved auth in the environment first.
    export_mlflow_auth_env(vars_)

    # Route every MLflow call (tracking, experiments, UC EvaluationDataset
    # reads, ...) through the target workspace's CLI profile. Plain
    # ``"databricks"`` would use the default profile, which is almost never
    # what you want when targets point at different workspaces.
    profile = vars_.get("profile")
    mlflow.set_tracking_uri(f"databricks://{profile}" if profile else "databricks")

    # Bug #30 workaround (MLflow <3.12.0): mlflow.genai.evaluate reads
    # UC-backed traces via a SQL warehouse; trace fetch silently returns
    # None when MLFLOW_TRACING_SQL_WAREHOUSE_ID is unset, which later
    # crashes scorers at ``eval_item.trace.info``.  Tracing config comes
    # from ``InstanceConfig`` (``foundry.toml`` + targets).  Fall back
    # to the target's ``warehouse_id`` so this works out of the box.
    warehouse_id = vars_.get("tracing_warehouse_id", "") or vars_.get("warehouse_id", "")
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


def run_trace_evaluation(
    config: dict[str, Any],
    instance_dir: Path | None = None,
    variables: dict[str, str] | None = None,
    max_traces: int = 100,
    dry_run: bool = False,
) -> int:
    """Run answer-sheet evaluation on existing production traces.

    Uses ``production_experiment`` (where the agent logs live traces),
    falling back to ``experiment_name`` if not set.
    """
    import mlflow

    experiment_name = _get_experiment_name(config, variables)
    _init_mlflow_experiment(experiment_name, variables)
    print(f"MLflow experiment: {experiment_name}")

    print(f"Fetching up to {max_traces} traces ...")
    traces = mlflow.search_traces(
        filter_string="attributes.status = 'OK'",
        max_results=max_traces,
    )
    print(f"  Found {len(traces)} traces")

    if len(traces) == 0:
        print("No traces found. Ensure your agent is logging traces to this experiment.")
        return 1

    scorers = resolve_scorers(config, has_expectations=False, traces_mode=True)
    print(f"  Scorers: {len(scorers)} ({', '.join(_scorer_name(s) for s in scorers)})")

    if dry_run:
        print(f"[DRY RUN] Would score {len(traces)} existing traces")
        return 0

    print(f"Scoring {len(traces)} traces ...")
    results = mlflow.genai.evaluate(
        data=traces,
        scorers=scorers,
    )
    _print_results(results)
    return 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_EVAL_HTTP_TIMEOUT_SECONDS_DEFAULT = 360


def _eval_http_timeout_seconds() -> int:
    """Resolve the HTTP read-timeout for the deployed predict_fn.

    Reads ``EVAL_HTTP_TIMEOUT_SECONDS`` (positive integer); falls back to
    ``_EVAL_HTTP_TIMEOUT_SECONDS_DEFAULT``. Matches the agent's observed
    p99 latency so the laptop doesn't cut off genuine slow answers (e.g.
    Genie calls under the 20 QPM workspace limit).
    """
    from agent_foundry import env

    value = (
        env.read_int(
            env.EVAL_HTTP_TIMEOUT_SECONDS,
            fallback=_EVAL_HTTP_TIMEOUT_SECONDS_DEFAULT,
        )
        or _EVAL_HTTP_TIMEOUT_SECONDS_DEFAULT
    )
    return value if value > 0 else _EVAL_HTTP_TIMEOUT_SECONDS_DEFAULT


def _uc_data_has_expectations(data: Any) -> bool:
    """Check whether UC dataset contains expectations columns."""
    if hasattr(data, "to_df"):
        df = data.to_df()
        return "expectations" in df.columns
    try:
        import pandas as pd

        if isinstance(data, pd.DataFrame):
            return "expectations" in data.columns
    except ImportError:
        pass
    return False


def _is_missing(value: Any) -> bool:
    """True for ``None`` or a NaN float -- the "missing" shapes pandas/UC columns use."""
    return value is None or (isinstance(value, float) and value != value)


def _json_default(value: Any) -> Any:
    """JSON encoder fallback for :func:`_request_extras`.

    A numpy scalar (``numpy.int64``, ``numpy.bool_``, ...) round-trips to
    its native Python type via ``.item()``; UC/pandas nested maps often
    carry these, and they are not JSON-serialisable nor ``int``/``bool``
    subclasses, so the plain ``default=str`` behaviour would silently turn
    ``1`` into ``"1"``. Anything else falls back to ``str``, as before.
    """
    try:
        import numpy as np

        if isinstance(value, np.generic):
            return value.item()
    except ImportError:
        pass
    return str(value)


def _request_extras(inputs: dict[str, Any]) -> dict[str, Any]:
    """Optional ``ResponsesAgentRequest`` fields a dataset row may carry.

    Returns the ``custom_inputs`` / ``context`` mappings found in a row's
    ``inputs``, ready to splat into the request.  A JSON-encoded mapping
    (as a UC dataset column may hold it) is decoded; a missing value
    (``None``, NaN, or a JSON ``null``) is silently skipped; any other
    non-mapping value is dropped with a warning.

    Each mapping is normalised through a JSON round-trip, so both predict
    modes send the same plain-JSON data (YAML dates become strings) and the
    agent gets its own copy rather than the dataset row's nested objects.
    ``context`` must also validate as MLflow's ``ChatContext``.
    """
    import json as _json
    from collections.abc import Mapping

    from mlflow.types.agent import ChatContext
    from pydantic import ValidationError

    extras: dict[str, Any] = {}
    for key in ("custom_inputs", "context"):
        value = inputs.get(key)
        if isinstance(value, str):
            try:
                value = _json.loads(value)
            except ValueError:
                pass
        if _is_missing(value):
            continue
        if not isinstance(value, Mapping):
            print(f"  WARNING: ignoring inputs.{key} (expected a mapping, got {type(value).__name__})")
            continue
        value = _json.loads(_json.dumps(dict(value), default=_json_default))
        if key == "context":
            try:
                ChatContext.model_validate(value)
            except ValidationError as exc:
                problems = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
                print(f"  WARNING: ignoring inputs.context (not a valid ChatContext: {problems})")
                continue
        extras[key] = value
    return extras


def _build_local_predict_fn(instance_dir: Path | None, variables: dict[str, str]) -> Callable[..., dict[str, Any]]:
    """Build a predict_fn that calls the registered ``@invoke()`` handler in-process.

    Uses ``get_invoke_function()`` from MLflow AgentServer -- the same
    pattern as the official Databricks agent template.  This runs the
    exact production code path (OBO fallback, tool creation, tracing)
    while giving MLflow full trace visibility (AGENT, TOOL, RETRIEVER spans).
    """
    import asyncio
    import os

    import nest_asyncio
    from mlflow.genai.agent_server import get_invoke_function
    from mlflow.types.responses import ResponsesAgentRequest

    from agent_foundry import env

    if instance_dir:
        os.environ.setdefault(env.INSTANCE_DIR.name, str(instance_dir))

    import payloads.agent.runtime.agent  # noqa: F401 — triggers @invoke() registration

    # ``mlflow.genai.agent_server.get_invoke_function`` is untyped
    # upstream; calling an untyped function in a typed context is
    # flagged by the framework-wide gate.  The runtime contract is
    # documented and stable; remove the ignore once mlflow ships
    # annotations for this module.
    invoke_fn = get_invoke_function()  # type: ignore[no-untyped-call]
    assert invoke_fn is not None, "No @invoke() handler found. Ensure payloads.agent.runtime.agent registers one."

    is_async = asyncio.iscoroutinefunction(invoke_fn)
    if is_async:
        nest_asyncio.apply()

    print(f"  Agent invoke handler loaded (async={is_async})")

    # MLflow calls ``predict_fn(**inputs)``. ``custom_inputs`` / ``context``
    # are the keys this runner forwards (see ``_request_extras``); any other
    # key a row's ``inputs`` carries (e.g. a UC struct that grows a new
    # field) is accepted and ignored here rather than raising ``TypeError``,
    # matching the deployed path's tolerance for unknown ``inputs.*`` keys.
    def predict_fn(
        question: str,
        custom_inputs: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
        **_unused_inputs: Any,
    ) -> dict[str, Any]:
        req = ResponsesAgentRequest(
            input=[{"role": "user", "content": question}],
            **_request_extras({"custom_inputs": custom_inputs, "context": context}),
        )
        if is_async:
            loop = asyncio.get_event_loop()
            response = loop.run_until_complete(invoke_fn(req))
        else:
            response = invoke_fn(req)

        if hasattr(response, "to_dict"):
            result: dict[str, Any] = response.to_dict()
            return result
        return {"output": str(response)}

    return predict_fn


def _as_records(data: Any) -> list[dict[str, Any]]:
    """Normalize an eval dataset (list-of-dicts or UC dataset) to records.

    UC ``EvaluationDataset`` objects are converted via ``.to_df()``. Anything
    else that already looks like a list-of-dicts is returned as-is.
    """
    if isinstance(data, list):
        records: list[dict[str, Any]] = data
        return records
    if hasattr(data, "to_df"):
        try:
            df_records: list[dict[str, Any]] = data.to_df().to_dict("records")
            return df_records
        except Exception as exc:
            print(f"  WARNING: Could not convert UC dataset to records: {exc}")
            return []
    try:
        import pandas as pd

        if isinstance(data, pd.DataFrame):
            pd_records: list[dict[str, Any]] = data.to_dict("records")
            return pd_records
    except ImportError:
        pass
    print(f"  WARNING: Unsupported dataset type for deployed trace eval: {type(data).__name__}")
    return []


_TRACE_ID_HEADER = "x-mlflow-return-trace-id"
_TRACE_POLL_INTERVALS_S = (3, 3, 5, 8, 10, 15, 30)


def _resolve_trace_warehouse_id(variables: dict[str, str]) -> str:
    """Warehouse MLflow needs to read UC OTel traces (bug #30)."""
    from agent_foundry import env

    published = variables.get("tracing_warehouse_id") or variables.get("warehouse_id") or ""
    from_env = env.read_optional(env.MLFLOW_TRACING_SQL_WAREHOUSE_ID) or ""
    return published.strip() or from_env.strip()


def _fetch_deployed_traces(
    records: list[dict[str, Any]],
    instance_dir: Path | None,
    variables: dict[str, str],
    *,
    ws: Any | None = None,
) -> list[dict[str, Any]]:
    """Call the deployed app for each record and return records with deep traces.

    Flow per record:
      1. POST ``/invocations`` with ``x-mlflow-return-trace-id: true`` so the
         MLflow AgentServer returns ``response.metadata.trace_id`` pointing at
         the UC OTel tables (see mlflow/mlflow#20387).  The row's optional
         ``inputs.custom_inputs`` / ``inputs.context`` mappings are sent as
         the request's ``custom_inputs`` / ``context`` fields.
      2. Poll ``mlflow.get_trace(trace_id)`` against UC (via
         ``MLFLOW_TRACING_SQL_WAREHOUSE_ID``) until the deep trace lands.
      3. Emit a ``{inputs, expectations, trace}`` record.

    ``mlflow.genai.evaluate`` skips its predict loop when the ``trace`` column
    is already populated, so scorers receive the deep deployed trace directly.

    Auth uses the laptop's interactive OAuth session (``.auth/callback``
    cookies), so the agent runs with real OBO.
    """
    import json as _json
    import os
    import time

    import mlflow
    import requests

    from agent_foundry import env

    if ws is None:
        raise RuntimeError("workspace client required to fetch deployed traces")

    # Pre-start the tracing warehouse so mlflow.get_trace against UC OTel
    # tables doesn't race with auto-termination. Known MLflow
    # issue (planned fix: MLflow 3.12.0). Bind from published variables
    # (same keys as live tracing checks), not only ambient env: eval's
    # load_brick_variables path does not pass ``ws``, so warehouse discovery
    # can miss the id that live checks then re-read from the workspace.
    warehouse_id = _resolve_trace_warehouse_id(variables)
    if not warehouse_id:
        print(
            "  ERROR: no tracing_warehouse_id or warehouse_id; cannot query UC traces (mlflow.get_trace returns None)."
        )
        return []
    os.environ[env.MLFLOW_TRACING_SQL_WAREHOUSE_ID.name] = warehouse_id
    print(f"  Ensuring warehouse {warehouse_id} is running ...")
    ws.warehouses.start_and_wait(id=warehouse_id)

    app_name = variables.get("app_resource_name") or variables.get("app_name", "")
    if not app_name and instance_dir:
        app_name = instance_dir.name

    app = ws.apps.get(name=app_name)
    invocations_url = f"{app.url.rstrip('/')}/invocations"
    auth_headers = ws.config.authenticate()

    http_timeout_s = _eval_http_timeout_seconds()
    print(f"  Targeting deployed app: {invocations_url} (HTTP timeout {http_timeout_s}s)")

    request_headers = {
        **auth_headers,
        "Content-Type": "application/json",
        _TRACE_ID_HEADER: "true",
    }

    enriched: list[dict[str, Any]] = []
    for idx, record in enumerate(records, start=1):
        inputs = record.get("inputs") or {}
        expectations = record.get("expectations") or {}
        question = inputs.get("question") if isinstance(inputs, dict) else None
        if not question:
            print(f"  [{idx}/{len(records)}] skipped — record has no inputs.question")
            continue

        try:
            resp = requests.post(
                invocations_url,
                json={
                    "input": [{"role": "user", "content": question}],
                    "stream": False,
                    **_request_extras(inputs),
                },
                headers=request_headers,
                timeout=http_timeout_s,
            )
            resp.raise_for_status()
        except Exception as exc:
            print(f"  [{idx}/{len(records)}] HTTP failed: {exc}")
            continue

        try:
            body = _json.loads(resp.content.decode("utf-8", errors="replace"))
        except Exception as exc:
            print(f"  [{idx}/{len(records)}] response not JSON: {exc}")
            continue

        trace_id = (body.get("metadata") or {}).get("trace_id")
        if not trace_id:
            print(
                f"  [{idx}/{len(records)}] server did not return metadata.trace_id "
                "(check x-mlflow-return-trace-id support)"
            )
            continue

        trace = None
        for wait_s in _TRACE_POLL_INTERVALS_S:
            time.sleep(wait_s)
            try:
                trace = mlflow.get_trace(trace_id, silent=True)
            except Exception as exc:
                print(f"  [{idx}/{len(records)}] get_trace error: {exc}")
                continue
            if trace is not None:
                break

        if trace is None:
            print(
                f"  [{idx}/{len(records)}] trace {trace_id[:60]}... never appeared "
                f"in UC after {sum(_TRACE_POLL_INTERVALS_S)}s"
            )
            continue

        print(f"  [{idx}/{len(records)}] trace fetched (question={question[:60]!r})")
        enriched.append(
            {
                "inputs": inputs,
                "expectations": expectations,
                "trace": trace,
            }
        )

    return enriched


def _scorer_name(s: Any) -> str:
    """Extract a readable name from a scorer."""
    if hasattr(s, "name"):
        return str(s.name)
    if hasattr(s, "__name__"):
        return str(s.__name__)
    return type(s).__name__


def _print_results(results: Any) -> None:
    """Print a summary of evaluation results."""
    print("\n=== Evaluation Results ===")
    if hasattr(results, "metrics") and results.metrics:
        for metric, value in results.metrics.items():
            if isinstance(value, float):
                print(f"  {metric}: {value:.4f}")
            else:
                print(f"  {metric}: {value}")
    else:
        print("  (No aggregate metrics returned)")
    print("View details in the MLflow experiment UI.\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run MLflow GenAI evaluation for an agent instance")
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
        "--mode",
        choices=["direct", "traces"],
        default="direct",
        help="Evaluation mode: direct (run agent) or traces (score existing)",
    )
    parser.add_argument(
        "--predict-via",
        choices=["local", "deployed"],
        default="deployed",
        help="How to run the agent in --mode direct: "
        "'deployed' (default) calls the live app HTTP endpoint with "
        "your laptop's user OAuth, then fetches the deep UC OTel trace -- "
        "real OBO, deep trace; "
        "'local' instantiates in-process -- deep trace, no OBO.",
    )
    parser.add_argument(
        "--dataset",
        help="UC EvaluationDataset or Delta table name (catalog.schema.table). Overrides YAML dataset discovery.",
    )
    parser.add_argument(
        "--max-traces",
        type=int,
        default=100,
        help="Maximum traces to fetch in --mode traces",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Load and validate only, don't run evaluation",
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

    from agent_foundry.telemetry import emit_command
    from agent_foundry.workspace_client import prefer_workspace_client

    ws = None if args.dry_run else prefer_workspace_client(variables)
    variables = fill_state_outputs(variables, instance_dir, args.target, ws)
    if not args.dry_run:
        emit_command("eval", ws=ws)

    config = load_eval_config(instance_dir, variables)

    if args.max_traces:
        config["max_traces"] = args.max_traces

    predict_via = getattr(args, "predict_via", "deployed")
    print(
        f"Evaluation: instance={args.instance} target={args.target} mode={args.mode}"
        + (f" predict_via={predict_via}" if args.mode == "direct" else "")
    )

    if args.mode == "direct":
        return run_direct_evaluation(
            config,
            instance_dir,
            variables,
            dataset_name=args.dataset,
            dry_run=args.dry_run,
            predict_via=predict_via,
            ws=ws,
        )
    else:
        return run_trace_evaluation(
            config,
            instance_dir=instance_dir,
            variables=variables,
            max_traces=config.get("max_traces", 100),
            dry_run=args.dry_run,
        )


if __name__ == "__main__":
    raise SystemExit(main())
