"""Fail fast on the removed ``conf/targets`` + ``instance.yml`` layout.

The only place in production code allowed to name the legacy files:
everything else reads ``foundry.toml`` exclusively.
"""

from __future__ import annotations

from pathlib import Path

from agent_foundry.loader import FoundryConfigError

LEGACY_TARGETS_GLOB = "conf/targets/*.yml"
LEGACY_INSTANCE_FILE = "instance.yml"
MIGRATION_DOC = "docs/reference/config.md#unsupported-layouts"


class LegacyLayoutError(FoundryConfigError):
    """Legacy config files are present; they are no longer read."""


def find_legacy_files(repo_root: Path, instances_root: Path) -> list[Path]:
    """Return every legacy config file under *repo_root* / *instances_root*."""
    found = sorted(repo_root.glob(LEGACY_TARGETS_GLOB))
    if instances_root.is_dir():
        found += sorted(instances_root.glob(f"*/{LEGACY_INSTANCE_FILE}"))
    return found


def reject_legacy_layout(repo_root: Path, instances_root: Path) -> None:
    """Raise :class:`LegacyLayoutError` if any legacy config file exists.

    The files would otherwise be silently ignored, leaving operators
    wondering why edits to them have no effect.
    """
    legacy = find_legacy_files(repo_root, instances_root)
    if not legacy:
        return
    listed = "\n".join(f"  - {path}" for path in legacy)
    raise LegacyLayoutError(
        "The conf/targets/*.yml + instance.yml layout is no longer supported "
        f"and these files are ignored:\n{listed}\n"
        f"Move their settings into foundry.toml and delete them (see {MIGRATION_DOC})."
    )


__all__ = ["LegacyLayoutError", "find_legacy_files", "reject_legacy_layout"]
