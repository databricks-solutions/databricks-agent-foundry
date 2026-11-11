"""The tracing experiment exists and the ``/invocations`` call left a fetchable trace."""

from __future__ import annotations

import os
import time
from typing import Any

import mlflow

from agent_foundry import env

# Traces reach the store asynchronously after the reply.
_TRACE_TIMEOUT_SECONDS = 180
_TRACE_POLL_SECONDS = 10


def bind_uc_trace_warehouse(variables: dict[str, str]) -> str:
    """Point ``mlflow.get_trace`` at the warehouse that can query UC OTel tables.

    Without ``MLFLOW_TRACING_SQL_WAREHOUSE_ID``, ``get_trace`` returns
    ``None`` even when the row exists (same workaround as evaluation,
    bug #30). Prefer the tracing brick's warehouse, then the
    instance warehouse.
    """
    warehouse_id = (variables.get("tracing_warehouse_id") or variables.get("warehouse_id") or "").strip()
    if not warehouse_id:
        raise LookupError("published variables have no tracing_warehouse_id or warehouse_id")
    os.environ[env.MLFLOW_TRACING_SQL_WAREHOUSE_ID.name] = warehouse_id
    return warehouse_id


def _wait_for_trace(trace_id: str) -> Any:
    deadline = time.monotonic() + _TRACE_TIMEOUT_SECONDS
    trace = mlflow.get_trace(trace_id, silent=True)
    while trace is None and time.monotonic() < deadline:
        time.sleep(_TRACE_POLL_SECONDS)
        trace = mlflow.get_trace(trace_id, silent=True)
    return trace


def test_tracing_experiment_exists(live_ws: Any, live_variables: dict[str, str]) -> None:
    experiment_id = live_variables.get("experiment_id")
    assert experiment_id, "tracing brick published no experiment_id"
    assert live_ws.experiments.get_experiment(experiment_id).experiment


def test_invocation_trace_lands(invocation: dict[str, Any], live_variables: dict[str, str]) -> None:
    trace_id = (invocation.get("metadata") or {}).get("trace_id")
    assert trace_id, f"/invocations returned no metadata.trace_id: {invocation}"
    bind_uc_trace_warehouse(live_variables)
    mlflow.set_tracking_uri("databricks")
    assert _wait_for_trace(trace_id) is not None, f"trace {trace_id} not found after {_TRACE_TIMEOUT_SECONDS}s"
