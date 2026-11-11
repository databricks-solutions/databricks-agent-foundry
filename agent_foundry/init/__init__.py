"""Implementation of ``agent-foundry init`` (issue #210).

The init wizard generates a new downstream application repo skeleton
outside the framework directory.  It collects workspace targets, a
VCS provider choice, and a starter-content template via interactive
prompts, then hands a fully-validated :class:`InitContext` to the
renderer (PR3) which writes the files.

This package is split into two pieces:

* :mod:`agent_foundry.init.wizard` -- prompt collection and input
  validation.  No file IO; the wizard is pure orchestration over the
  :class:`InitContext` Pydantic model so unit tests can drive it
  with synthetic input via ``typer.testing.CliRunner``.
* :mod:`agent_foundry.init.renderer` (PR3) -- pure file-IO that takes
  a validated :class:`InitContext` and writes the generated repo.

Keeping wizard logic and file IO in separate modules means the
wizard can be tested without touching the filesystem, and the
renderer can be tested with hand-built contexts without driving
prompts.  See #210 for the full PR plan.
"""

from __future__ import annotations

from agent_foundry.init.renderer import InitRenderError, render
from agent_foundry.init.wizard import (
    CIProvider,
    InitContext,
    StarterTemplate,
    TargetSpec,
    collect,
)

__all__ = [
    "CIProvider",
    "InitContext",
    "InitRenderError",
    "StarterTemplate",
    "TargetSpec",
    "collect",
    "render",
]
