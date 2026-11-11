"""Generic framework envelope for ``payloads/<payload>/payload.yml``.

The ``app`` brick deploys generic Databricks App *infrastructure*; what
runs *inside* the container is the **payload**, selected and configured by
this file:

* ``payload.adapter`` names the payload adapter -- the package that knows
  how to translate the payload's ``spec`` into a neutral
  :class:`agent_foundry.deploy.app_host.AppPayloadSpec`.
* ``payload.requires`` lists framework bricks this payload needs at
  deploy time (e.g. ``warehouse``, ``tracing``, ``observability``).  The orchestrator
  skips bricks that are not required and errors when a required brick
  is missing from the instance or explicitly ``--skip``'d.
* ``payload.spec`` is a free-form, *adapter-owned* body. The framework
  passes it through to the named adapter without interpreting it, so a
  new payload can expose a different config surface without a framework
  change.

This module is the adapter-NEUTRAL envelope: it validates only the
top-level wrappers (``extra="forbid"``) and keeps ``spec`` an open
mapping.  The agent adapter's typed view of ``spec`` (runtime / ui /
integrations) is payload-owned and lives in :mod:`payloads.agent.contracts`
(:class:`~payloads.agent.contracts.AgentPayloadSpec`); that is what the
payload-owned JSON Schema and deploy-time validation use.  The framework
never imports it -- it stays agent-agnostic.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# Brick names a payload may declare as deploy-time requirements.
# Add a name here when a new brick is opt-in via ``payload.requires``.
PayloadBrickRequirement = Literal["warehouse", "tracing", "observability"]


class PayloadConfig(BaseModel):
    """The ``payload:`` block -- adapter selector + open ``spec`` body."""

    model_config = ConfigDict(extra="forbid")

    adapter: str
    requires: list[PayloadBrickRequirement] = Field(default_factory=list)
    spec: dict[str, Any] = {}


class PayloadFile(BaseModel):
    """Top-level shape of ``payload.yml`` (wraps :class:`PayloadConfig`).

    Wrapping the payload under a ``payload:`` key (rather than hoisting
    ``adapter:`` to the root) leaves room for future app-level payload
    options without breaking the schema.
    """

    model_config = ConfigDict(extra="forbid")

    payload: PayloadConfig


__all__ = ["PayloadBrickRequirement", "PayloadConfig", "PayloadFile"]
