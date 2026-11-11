"""Typed return value for :func:`agent_foundry.load_state_outputs`.

A typed Pydantic wrapper around the flat ``dict[str, str]`` of state
outputs that adds provenance (which bricks contributed).

Why no generic ``StateOutputs[T]``?
-----------------------------------

Issue #159 originally proposed a generic wrapper parametrized by the
brick's ``state_schema`` Pydantic model.  In practice, the framework's
state-output contract is **flat string variables**, not full state
objects: bricks publish their ``state_schema``-shaped state to the
workspace, but their ``load_state_outputs`` return
values are deliberately flattened to ``{var_name: var_value}`` strings
that downstream bricks consume via ``${var.foo}`` interpolation in
YAML.  A ``StateOutputs[GenieSpaceState]`` generic would lie about the
runtime shape -- the actual values are never the typed state object,
only flat strings derived from it.

So this module ships a single concrete :class:`StateOutputs` model
that wraps the flat-string dict and adds provenance metadata.  The
``state_schema`` enforcement work (per-brick state file validation)
lives in PR #156, where it logically belongs.

Merging into a variable bag
---------------------------

Use ``variables.update(load_state_outputs(...).variables)``.
``dict.update(model)`` would yield the model's *fields*
(``variables``, ``bricks_loaded``) rather than the inner variable
key/value pairs.  Always go through :attr:`variables` explicitly.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class StateOutputs(BaseModel):
    """Typed wrapper around the flat variable dict produced by
    :func:`agent_foundry.load_state_outputs`.

    :attr:`variables` is the canonical payload: a flat mapping of
    ``var_name -> var_value`` strings, ready to merge into the
    deploy-time variables dict that the orchestrator threads through
    each brick's :class:`~agent_foundry.bricks.DeployContext`.

    :attr:`bricks_loaded` records which registered bricks contributed
    state during the aggregation, for logging / debugging.  A brick
    with no state file (or whose state file did not yet exist on disk)
    is *not* listed here even though it was visited.
    """

    model_config = ConfigDict(extra="forbid")

    variables: dict[str, str]
    bricks_loaded: list[str]
