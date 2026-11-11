"""Instance directory marker helper (issue #295).

A directory is an application instance when it contains a per-instance
``foundry.toml``.  This module is the single source of truth for that
decision so every call site -- runtime, deploy CLI, validate,
orchestrator -- behaves identically.
"""

from __future__ import annotations

from pathlib import Path

INSTANCE_MARKER: str = "foundry.toml"


def is_instance_dir(directory: Path) -> bool:
    """Return ``True`` when *directory* contains the instance marker."""
    return (directory / INSTANCE_MARKER).is_file()


__all__ = [
    "INSTANCE_MARKER",
    "is_instance_dir",
]
