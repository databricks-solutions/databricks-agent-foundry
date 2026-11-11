"""
Shared variable resolution for all bricks and the orchestrator.

Provides a single source of truth for:
  - Substituting ${var.name} placeholders in strings and nested structures
  - The standard ``--project-root`` argparse arg used by every brick
    script's standalone CLI entrypoint.

Every brick script and the orchestrator import from here instead of
maintaining their own copies of these utilities.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Any

_VAR_RE = re.compile(r"\$\{var\.([^}]+)\}")


def add_project_root_arg(parser: argparse.ArgumentParser) -> None:
    """Register the standard ``--project-root`` argument on *parser*.

    Used by every brick's standalone CLI entrypoint
    (``python -m bricks.<x>.scripts.<y>``).  Defaults to
    ``Path.cwd().resolve()``; the deploy orchestrator path never calls
    these ``main(argv)`` entrypoints (it invokes the script's library
    function directly with paths already resolved -- see
    ``Brick.deploy(ctx)``), so this default is only exercised by
    direct CLI invocations.
    """
    parser.add_argument(
        "--project-root",
        type=lambda s: Path(s).resolve(),
        default=Path.cwd().resolve(),
        help="Repo root containing foundry.toml and instances/ (default: CWD).",
    )


def substitute(text: str, variables: dict[str, str], *, strict: bool = True) -> str:
    """Replace ``${var.name}`` placeholders in *text*.

    When *strict* is True (default), raises ``ValueError`` for references to
    undefined variables.  When False, leaves them as-is.
    """

    def _replacer(match: re.Match) -> str:
        key = match.group(1)
        if key not in variables:
            if strict:
                raise ValueError(f"Undefined variable: ${{var.{key}}}")
            return match.group(0)
        return variables[key]

    return _VAR_RE.sub(_replacer, text)


def substitute_obj(obj: Any, variables: dict[str, str], *, strict: bool = True) -> Any:
    """Recursively substitute ``${var.name}`` in a nested structure."""
    if isinstance(obj, dict):
        return {k: substitute_obj(v, variables, strict=strict) for k, v in obj.items()}
    if isinstance(obj, list):
        return [substitute_obj(item, variables, strict=strict) for item in obj]
    if isinstance(obj, str):
        return substitute(obj, variables, strict=strict)
    return obj
