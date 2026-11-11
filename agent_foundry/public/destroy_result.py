"""Typed return value for :func:`agent_foundry.destroy_instance`."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from agent_foundry.bricks import BrickResult, DestroyPlan


class DestroyResult(BaseModel):
    """Typed result of one :func:`agent_foundry.destroy_instance` call."""

    model_config = ConfigDict(extra="forbid")

    instance_name: str
    target: str
    bricks: dict[str, list[BrickResult]]
    plan: DestroyPlan
    forced: bool
    duration_seconds: float = 0.0
