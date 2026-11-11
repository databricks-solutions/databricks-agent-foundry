"""The observability dashboard recorded in workspace state exists and is active."""

from __future__ import annotations

from typing import Any

from databricks.sdk.service.dashboards import LifecycleState

from agent_foundry.state import load_state
from agent_foundry.testing.live import LiveTarget

# bricks/observability keeps its state under brick type "dashboard", keyed by instance.
_STATE_BRICK_TYPE = "dashboard"


def test_dashboard_is_active(live_ws: Any, live_target: LiveTarget) -> None:
    instance = live_target.instance
    state = load_state(live_ws, instance, _STATE_BRICK_TYPE, instance, live_target.binding.target)
    dashboard_id = state.get("dashboard_id")
    assert dashboard_id, f"no observability dashboard state for {instance}"
    assert live_ws.lakeview.get(dashboard_id).lifecycle_state == LifecycleState.ACTIVE
