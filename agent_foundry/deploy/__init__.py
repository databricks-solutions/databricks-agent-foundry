"""
Public deployment API
======================
``agent_foundry.deploy`` exposes the framework's deployment surface as a
library-grade API so downstream consumers can orchestrate deployments
from their own code (CI jobs, custom scripts, notebooks) without
depending on the ``scripts/deploy_all.py`` shim at the repo root.

Public symbols:

- :func:`deploy_instance`    -- deploy all bricks for a single instance
- :func:`destroy_instance`   -- tear down resources for a single instance
- :func:`discover_instances` -- list ``foundry.toml``-bearing dirs

Everything else in :mod:`agent_foundry.deploy._orchestrator` is internal
and **not** part of the SemVer-stable surface.

Companion CLI: ``agent-foundry deploy`` (see :mod:`agent_foundry.deploy.cli`).
"""

from __future__ import annotations

from agent_foundry.deploy._orchestrator import (
    deploy_instance,
    destroy_instance,
    discover_instances,
)

__all__ = [
    "deploy_instance",
    "destroy_instance",
    "discover_instances",
]
