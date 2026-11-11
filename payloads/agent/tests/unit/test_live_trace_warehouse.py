"""Warehouse binding for the live ``get_trace`` check."""

from __future__ import annotations

import os

import pytest

from agent_foundry import env
from payloads.agent.tests.live.test_tracing import bind_uc_trace_warehouse


def test_bind_prefers_tracing_warehouse(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(env.MLFLOW_TRACING_SQL_WAREHOUSE_ID.name, raising=False)
    warehouse_id = bind_uc_trace_warehouse(
        {"tracing_warehouse_id": "tr-1", "warehouse_id": "wh-2"},
    )
    assert warehouse_id == "tr-1"
    assert os.environ[env.MLFLOW_TRACING_SQL_WAREHOUSE_ID.name] == "tr-1"


def test_bind_falls_back_to_instance_warehouse(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(env.MLFLOW_TRACING_SQL_WAREHOUSE_ID.name, raising=False)
    warehouse_id = bind_uc_trace_warehouse({"warehouse_id": "wh-2"})
    assert warehouse_id == "wh-2"
    assert os.environ[env.MLFLOW_TRACING_SQL_WAREHOUSE_ID.name] == "wh-2"


def test_bind_requires_a_warehouse_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(env.MLFLOW_TRACING_SQL_WAREHOUSE_ID.name, raising=False)
    with pytest.raises(LookupError, match="warehouse_id"):
        bind_uc_trace_warehouse({})
