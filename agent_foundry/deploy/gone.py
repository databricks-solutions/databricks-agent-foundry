"""Destroy means gone: wait until every resource a brick deleted has disappeared.

Several deletes are asynchronous or only become visible after a delay
(vector indexes, Apps, a trashed Genie space). Bricks report what they
deleted in :attr:`BrickResult.deleted`; :func:`verify_gone` polls each
resource with the check for its kind and turns a result into
``destroy_error`` when a resource outlives its timeout.

"Gone" follows each service's delete semantics: trashed (Genie spaces,
dashboards), soft-deleted (MLflow experiments, Lakebase projects) and
``DELETED`` warehouses count as gone.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from databricks.sdk.errors import NotFound
from databricks.sdk.errors.base import DatabricksError

from agent_foundry.bricks import BrickResult, ResourceKind, ResourceRef

_GONE_MARKERS = ("does not exist", "not found", "trashed")
_MCP_SERVICES_API = "/api/2.1/unity-catalog/mcp-services"

DEFAULT_TIMEOUT_SECONDS = 5 * 60
POLL_SECONDS = 10
_TIMEOUT_SECONDS: dict[ResourceKind, int] = {
    "app": 15 * 60,
    "lakebase_project": 15 * 60,
    "serving_endpoint": 15 * 60,
    "vector_index": 15 * 60,
}


def _lookup(fetch: Callable[..., Any], **kwargs: Any) -> Any | None:
    """Return the resource, or ``None`` when the service says it is gone."""
    try:
        return fetch(**kwargs)
    except NotFound:
        return None
    except DatabricksError as exc:
        if any(marker in str(exc).lower() for marker in _GONE_MARKERS):
            return None
        raise


def _genie_space_gone(ws: Any, resource_id: str) -> bool:
    return _lookup(ws.genie.get_space, space_id=resource_id) is None


def _vector_index_gone(ws: Any, resource_id: str) -> bool:
    return _lookup(ws.vector_search_indexes.get_index, index_name=resource_id) is None


def _job_gone(ws: Any, resource_id: str) -> bool:
    return _lookup(ws.jobs.get, job_id=int(resource_id)) is None


def _uc_schema_gone(ws: Any, resource_id: str) -> bool:
    return _lookup(ws.schemas.get, full_name=resource_id) is None


def _uc_function_gone(ws: Any, resource_id: str) -> bool:
    return _lookup(ws.functions.get, name=resource_id) is None


def _uc_connection_gone(ws: Any, resource_id: str) -> bool:
    return _lookup(ws.connections.get, name=resource_id) is None


def _app_gone(ws: Any, resource_id: str) -> bool:
    return _lookup(ws.apps.get, name=resource_id) is None


def _mcp_service_gone(ws: Any, resource_id: str) -> bool:
    return _lookup(ws.api_client.do, method="GET", path=f"{_MCP_SERVICES_API}/{resource_id}") is None


def _skill_gone(ws: Any, resource_id: str) -> bool:
    return _lookup(ws.ai_gateway.get_skill, name=f"skills/{resource_id}") is None


def _sql_warehouse_gone(ws: Any, resource_id: str) -> bool:
    warehouse = _lookup(ws.warehouses.get, id=resource_id)
    return warehouse is None or getattr(warehouse.state, "value", None) == "DELETED"


def _dashboard_gone(ws: Any, resource_id: str) -> bool:
    dashboard = _lookup(ws.lakeview.get, dashboard_id=resource_id)
    return dashboard is None or getattr(dashboard.lifecycle_state, "value", None) == "TRASHED"


def _mlflow_experiment_gone(ws: Any, resource_id: str) -> bool:
    response = _lookup(ws.experiments.get_experiment, experiment_id=resource_id)
    return response is None or response.experiment.lifecycle_stage == "deleted"


def _serving_endpoint_gone(ws: Any, resource_id: str) -> bool:
    return _lookup(ws.serving_endpoints.get, name=resource_id) is None


def _registered_model_gone(ws: Any, resource_id: str) -> bool:
    return _lookup(ws.registered_models.get, full_name=resource_id) is None


def _lakebase_project_gone(ws: Any, resource_id: str) -> bool:
    project = _lookup(ws.postgres.get_project, name=resource_id)
    return project is None or project.delete_time is not None


_GONE_CHECKS: dict[ResourceKind, Callable[[Any, str], bool]] = {
    "app": _app_gone,
    "dashboard": _dashboard_gone,
    "genie_space": _genie_space_gone,
    "job": _job_gone,
    "lakebase_project": _lakebase_project_gone,
    "mcp_service": _mcp_service_gone,
    "mlflow_experiment": _mlflow_experiment_gone,
    "registered_model": _registered_model_gone,
    "serving_endpoint": _serving_endpoint_gone,
    "skill": _skill_gone,
    "sql_warehouse": _sql_warehouse_gone,
    "uc_connection": _uc_connection_gone,
    "uc_function": _uc_function_gone,
    "uc_schema": _uc_schema_gone,
    "vector_index": _vector_index_gone,
}


def is_gone(ws: Any, ref: ResourceRef) -> bool:
    """One check of whether *ref* no longer exists."""
    return _GONE_CHECKS[ref.kind](ws, ref.resource_id)


def wait_until_gone(
    ws: Any,
    refs: list[ResourceRef],
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> list[ResourceRef]:
    """Poll until every ref is gone; return the refs still present at their kind's timeout."""
    start = clock()
    pending = list(refs)
    expired: list[ResourceRef] = []
    while pending:
        pending = [ref for ref in pending if not is_gone(ws, ref)]
        elapsed = clock() - start
        expired += [ref for ref in pending if elapsed >= _TIMEOUT_SECONDS.get(ref.kind, DEFAULT_TIMEOUT_SECONDS)]
        pending = [ref for ref in pending if ref not in expired]
        if pending:
            sleep(POLL_SECONDS)
    return expired


def _as_destroy_error(result: BrickResult, **details: Any) -> BrickResult:
    return result.model_copy(update={"status": "destroy_error", "rc": 1, "details": {**result.details, **details}})


def _verify_result(
    ws: Any,
    result: BrickResult,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> BrickResult:
    if result.status != "ok" or not result.deleted:
        return result
    try:
        remaining = wait_until_gone(ws, result.deleted, clock=clock, sleep=sleep)
    except Exception as exc:
        print(f"  ERROR: could not verify deletion: {exc}")
        return _as_destroy_error(result, error=f"could not verify deletion: {exc}")
    if not remaining:
        return result
    still = [f"{ref.kind} {ref.resource_id}" for ref in remaining]
    print(f"  ERROR: still present after destroy: {', '.join(still)}")
    return _as_destroy_error(result, still_exists=still)


def verify_gone(
    ws: Any,
    results: list[BrickResult],
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> list[BrickResult]:
    """Return *results* with any ok result whose deleted resources outlive their timeout marked ``destroy_error``."""
    return [_verify_result(ws, result, clock, sleep) for result in results]
