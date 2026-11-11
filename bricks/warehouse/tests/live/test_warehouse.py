"""The published ``warehouse_id`` runs SQL."""

from __future__ import annotations

from typing import Any

from agent_foundry.testing.live import run_sql


def test_published_warehouse_runs_sql(live_ws: Any, live_variables: dict[str, str]) -> None:
    warehouse_id = live_variables.get("warehouse_id", "")
    assert warehouse_id, "warehouse brick published no warehouse_id"
    assert run_sql(live_ws, warehouse_id, "SELECT 1") == [["1"]]
