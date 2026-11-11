"""Optional destroy helpers for brick authors (not part of the public contract)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from agent_foundry.bricks import (
    BrickResult,
    DestroyContext,
)
from agent_foundry.bricks import (
    iter_nested_sub_instances as iter_nested_sub_instances,
)
from agent_foundry.state import delete_state, load_state, validate_state

NOTHING_TO_DESTROY = "nothing to destroy"


def load_nested_state(
    ctx: DestroyContext,
    *,
    brick_type: str,
    sub_instance: str,
    schema: type[BaseModel] | None,
    brick_name: str,
) -> dict[str, Any]:
    """Load workspace-backed state for one sub-instance (``{}`` offline)."""
    state = load_state(ctx.ws, ctx.instance_dir.name, brick_type, sub_instance, ctx.target)
    if state:
        validate_state(brick_name, state, schema, sub_instance=sub_instance)
    return state


def pick_any_running_warehouse(ws: Any) -> str | None:
    """Return the id of any RUNNING SQL warehouse, or ``None``.

    Stateless workspace primitive -- no brick knowledge, no config files,
    no state.  Bricks that need a SQL execution context during destroy
    (e.g. dropping UC schemas/functions) can call this when their
    preferred ``warehouse_id`` is empty, e.g. because the dedicated
    warehouse was already torn down in a previous run.

    Only returns warehouses already in ``RUNNING`` state -- never
    auto-starts a stopped one, to avoid surprise cost or behaviour.

    Returns ``None`` when ``ws`` is ``None`` (dry-run / no client) or
    when the SDK call raises / yields no eligible warehouse.
    """
    if ws is None:
        return None
    try:
        for wh in ws.warehouses.list():
            state = getattr(wh, "state", None)
            state_value = getattr(state, "value", state)
            if str(state_value).upper() == "RUNNING":
                wh_id = getattr(wh, "id", None)
                if wh_id:
                    return str(wh_id)
    except Exception:
        return None
    return None


def nothing_to_destroy(name: str) -> BrickResult:
    """Skip result for a resource that does not exist (never deployed or already gone)."""
    return BrickResult(name=name, status="skipped", rc=0, details={"reason": NOTHING_TO_DESTROY})


def uc_object_exists(lookup: Callable[[str], Any], full_name: str) -> bool:
    """False only when Unity Catalog reports *full_name* as not found.

    *lookup* is the SDK getter, e.g. ``ws.schemas.get``.  Any other error
    (permissions, transient) counts as existing so destroy still attempts
    the drop instead of silently leaking the object.
    """
    from databricks.sdk.errors import NotFound

    try:
        lookup(full_name)
    except NotFound:
        return False
    except Exception:
        return True
    return True


def delete_workspace_state(
    ctx: DestroyContext,
    *,
    brick_type: str,
    sub_instance: str,
) -> None:
    """Remove a workspace-backed state file (preserves audit semantics)."""
    delete_state(
        ctx.ws,
        ctx.instance_dir.name,
        brick_type,
        sub_instance,
        ctx.target,
    )
