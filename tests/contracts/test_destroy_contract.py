"""Tests for the unified brick destroy contract."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_foundry.bricks import (
    Brick,
    BrickDestroyDefaults,
    BrickResult,
    DestroyContext,
    DestroyPlan,
    DestroyPlanItem,
    iter_bricks,
)


class _StubBrick(BrickDestroyDefaults):
    name = "stub_test_brick"
    display_name = "Stub"
    deploy_order = 999
    state_schema = None

    def applies_to(self, ctx) -> bool:  # noqa: ANN001, ARG002
        return True

    def deploy(self, ctx) -> list[BrickResult]:  # noqa: ANN001, ARG002
        return []


def test_every_builtin_brick_satisfies_protocol():
    for brick in iter_bricks():
        assert isinstance(brick, Brick)


def test_brick_destroy_defaults_returns_skipped():
    brick = _StubBrick()
    ctx = DestroyContext(instance_dir=Path("/tmp/x"), target="dev")
    assert brick.plan_destroy(ctx) == DestroyPlan()
    results = brick.destroy(ctx)
    assert len(results) == 1
    assert results[0].status == "skipped"
    assert results[0].details.get("reason") == "brick does not implement destroy"


def test_destroy_plan_item_extra_forbid():
    with pytest.raises(Exception):
        DestroyPlanItem(
            brick="warehouse",
            resource="wh",
            destructive=False,
            unexpected=True,  # type: ignore[call-arg]
        )


def test_sql_warehouse_brick_has_destroy():
    from bricks.warehouse.brick import SqlWarehouseBrick

    brick = SqlWarehouseBrick()
    ctx = DestroyContext(instance_dir=Path("/tmp/nope"), target="dev", dry_run=True)
    plan = brick.plan_destroy(ctx)
    assert plan.items == []
    results = brick.destroy(ctx)
    assert results[0].status == "skipped"


@pytest.mark.parametrize(
    "brick_path",
    [
        "bricks.observability.brick:ObservabilityBrick",
        "bricks.tracing.brick:MlflowExperimentBrick",
        "bricks.warehouse.brick:SqlWarehouseBrick",
    ],
)
def test_never_deployed_instance_is_skipped_with_reason(brick_path: str, tmp_path: Path) -> None:
    """Nothing to destroy reads ``[SKIP]`` with a reason, not ``[OK]`` (#542)."""
    import importlib

    from agent_foundry.destroy_base import NOTHING_TO_DESTROY

    module_name, class_name = brick_path.split(":")
    brick = getattr(importlib.import_module(module_name), class_name)()
    ctx = DestroyContext(instance_dir=tmp_path / "never-deployed", target="dev", dry_run=True, force=True)

    results = brick.destroy(ctx)

    assert [(r.status, r.details) for r in results] == [("skipped", {"reason": NOTHING_TO_DESTROY})]
