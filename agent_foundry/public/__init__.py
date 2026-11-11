"""Public typed return-value models for the agent-foundry framework.

Issue #159 introduced this module to replace loose ``dict[str, Any]``
return values on the public API surface (:func:`agent_foundry.deploy_instance`
and :func:`agent_foundry.load_state_outputs`) with typed Pydantic
models.  Downstream consumers get autocomplete and mypy coverage; the
framework gets a stable contract that future refactors can't silently
break.

Module conventions
------------------

* Everything re-exported here is the documented public surface for
  return values.  These names follow SemVer once the package is
  published as a library (issue #66).
* All models are Pydantic v2 ``BaseModel`` subclasses
  (``model.model_dump()`` gives the plain dict).
* This module is the *only* part of ``agent_foundry/`` checked with
  ``mypy --strict``.  Retrofitting strict mode on the rest of the
  framework is out of scope for #159.
"""

from __future__ import annotations

from agent_foundry.public.deploy_result import DeployResult
from agent_foundry.public.destroy_result import DestroyResult
from agent_foundry.public.foundry_config import (
    CURRENT_SCHEMA_VERSION,
    FoundryInstanceConfig,
    FoundryRepoConfig,
    InstanceRef,
    Target,
    Warehouse,
)
from agent_foundry.public.state_outputs import StateOutputs

__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "DeployResult",
    "DestroyResult",
    "FoundryInstanceConfig",
    "FoundryRepoConfig",
    "InstanceRef",
    "StateOutputs",
    "Target",
    "Warehouse",
]
