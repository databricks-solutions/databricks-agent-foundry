"""``ensure_experiment`` creates the trace schema MLflow links into (MLflow only creates the tables)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from databricks.sdk.errors import NotFound, PermissionDenied

from bricks.tracing.scripts.ensure_experiment import _ensure_trace_schema, ensure_experiment

_VARIABLES = {
    "tracing_experiment": "/Shared/x/experiments/dev-x-traces",
    "tracing_destination": "cat.x_agent",
}


class _Schemas:
    def __init__(self, *, exists: bool, create_error: Exception | None = None) -> None:
        self.exists = exists
        self.create_error = create_error
        self.created: list[tuple[str, str]] = []

    def get(self, *, full_name: str) -> Any:
        if not self.exists:
            raise NotFound(f"Schema '{full_name}' does not exist.")
        return object()

    def create(self, *, name: str, catalog_name: str) -> Any:
        if self.create_error is not None:
            raise self.create_error
        self.created.append((catalog_name, name))
        return object()


class _Ws:
    def __init__(self, schemas: _Schemas) -> None:
        self.schemas = schemas


def test_creates_missing_schema() -> None:
    schemas = _Schemas(exists=False)

    _ensure_trace_schema(_Ws(schemas), "cat", "x_agent")

    assert schemas.created == [("cat", "x_agent")]


def test_existing_schema_is_left_alone() -> None:
    schemas = _Schemas(exists=True)

    _ensure_trace_schema(_Ws(schemas), "cat", "x_agent")

    assert schemas.created == []


def test_create_failure_fails_deploy_before_mlflow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("bricks.tracing.scripts.ensure_experiment.export_mlflow_auth_env", lambda _v: None)
    ws = _Ws(_Schemas(exists=False, create_error=PermissionDenied("no CREATE SCHEMA on cat")))

    assert ensure_experiment(tmp_path, "dev", dict(_VARIABLES), ws=ws) == 1


def test_dry_run_does_not_touch_schema(tmp_path: Path) -> None:
    schemas = _Schemas(exists=False)

    assert ensure_experiment(tmp_path, "dev", dict(_VARIABLES), dry_run=True, ws=_Ws(schemas)) == 0
    assert schemas.created == []
