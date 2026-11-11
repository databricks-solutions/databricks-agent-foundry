"""Typed return value for :func:`agent_foundry.deploy_instance`.

A typed Pydantic model so downstream consumers (sample-agent CI
scripts, eval harnesses, etc.) can introspect the per-brick deploy
outcome with autocomplete and mypy coverage.

The wrapper carries:

* :attr:`bricks` -- ``{display_name: [BrickResult, ...]}``, one entry per
  registered brick that was visited during the deploy (including
  bricks that returned no sub-instances or were skipped).

Plus two metadata fields useful for downstream logging and audit:

* :attr:`instance_name` -- the deployed instance's directory name.
* :attr:`target` -- the deploy target (e.g. ``dev``, ``staging``).

Pydantic v2's natural iter protocol means ``dict(deploy_result)``
yields the model's *fields* (``instance_name``, ``target``, ``bricks``)
-- not the per-brick entries.  Use :attr:`bricks` explicitly.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from agent_foundry.bricks import BrickResult


class DeployResult(BaseModel):
    """Typed result of one :func:`agent_foundry.deploy_instance` call.

    Per-brick results are keyed by the brick's ``display_name`` (the
    same human-readable label the orchestrator prints in
    ``--- <display_name> ---`` headers), and each value is the list of
    :class:`~agent_foundry.bricks.BrickResult` objects that the brick
    emitted (one per sub-instance).
    """

    model_config = ConfigDict(extra="forbid")

    instance_name: str
    target: str
    bricks: dict[str, list[BrickResult]]
