"""Declarative scaffold descriptors (issue #368).

A :class:`ScaffoldDecl` describes one scaffoldable item -- its CLI name,
grouping and help copy, plus the concrete copy operations
(:class:`ScaffoldSource`) that materialise it into an instance directory.

Both halves of the scaffold surface produce these:

* the framework builds them for its own bricks (``warehouse``, ``app``,
  ...), and
* each :class:`~agent_foundry.deploy.payload_adapters.AppPayloadAdapter`
  declares its own via ``scaffolds()`` (e.g. the agent payload's
  ``payload`` / ``skills`` / ``mcp``).

:mod:`agent_foundry.scaffold` composes both into a single registry and
renders them generically, so a new payload can add scaffold verbs
without any framework edit.

This module holds only inert value types (no IO, no payload imports) so
it can be imported from the framework, the payload adapters, and the
scaffold renderer without introducing import cycles.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

ScaffoldShape = Literal["file", "tree"]


@dataclass(frozen=True)
class ScaffoldSource:
    """One copy operation: absolute *source* -> instance-relative *destination*.

    ``shape`` selects the copy strategy:

    * ``"file"`` -- *source* is a single file copied to *destination*.
    * ``"tree"`` -- *source* is a directory whose contents are copied
      recursively under *destination*.
    """

    source: Path
    destination: str
    shape: ScaffoldShape


@dataclass(frozen=True)
class ScaffoldDecl:
    """A single scaffoldable item in the CLI (``agent-foundry scaffold <name>``).

    ``name`` is what the user types; ``group`` places it in the grouped
    ``scaffold --list`` output (e.g. ``"framework"`` or ``"agent payload"``);
    ``display`` / ``summary`` are the human-facing label and one-line
    description.  ``sources`` is the ordered set of copy operations that
    materialise the scaffold (more than one for multi-part shapes such as
    the skills domain + skill pair).
    """

    name: str
    group: str
    display: str
    summary: str
    sources: tuple[ScaffoldSource, ...]
