"""After destroy, the warehouse the canary provisioned is deleted."""

from __future__ import annotations

from typing import Any

import pytest
from databricks.sdk.errors import NotFound
from databricks.sdk.service.sql import State

pytestmark = pytest.mark.gone

_GONE_STATES = frozenset({State.DELETED, State.DELETING})


def test_warehouse_is_deleted(live_ws: Any, gone_variables: dict[str, str]) -> None:
    warehouse_id = gone_variables.get("warehouse_id", "")
    assert warehouse_id, "no warehouse_id was published before destroy"
    try:
        state = live_ws.warehouses.get(warehouse_id).state
    except NotFound:
        return
    assert state in _GONE_STATES, f"warehouse {warehouse_id} is still {state}"
