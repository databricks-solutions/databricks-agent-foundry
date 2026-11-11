"""Scenario fixtures: files a canary adds to its instance after the scaffolds.

``fixtures/<scenario id>/`` mirrors ``instances/<name>/``.  Files are only
added, never replaced, so a fixture cannot hide what a scaffold writes.

The one exception is :data:`PAYLOAD_PATCH_FILENAME`: it is not copied but
merged into the scaffolded ``payloads/agent/payload.yml``, so a canary can
run with non-default knobs (e.g. lowered context thresholds) while every
other value still comes from the scaffold.
"""

from __future__ import annotations

import shutil
from importlib import resources as _resources
from pathlib import Path
from typing import Any, Final

import yaml

_FIXTURES_DIR: Final[str] = "fixtures"
PAYLOAD_PATCH_FILENAME: Final[str] = "payload.patch.yml"
_PAYLOAD_PATH: Final[Path] = Path("payloads/agent/payload.yml")


class FixtureError(ValueError):
    """Missing fixture, a fixture file that would replace an instance file, or an unusable payload patch."""


def fixture_dir(scenario_id: str) -> Path:
    """The bundled fixture tree for *scenario_id*."""
    root = _resources.files("agent_foundry.testing").joinpath(_FIXTURES_DIR, scenario_id)
    path = Path(str(root))
    if not path.is_dir():
        raise FixtureError(f"no fixture for scenario {scenario_id!r} at {path}")
    return path


def apply_fixture(source: Path, instance_dir: Path) -> list[Path]:
    """Copy every file under *source* into *instance_dir*; returns the instance-relative paths.

    Nothing is copied when any target already exists.  A root-level
    :data:`PAYLOAD_PATCH_FILENAME` is merged into the instance payload
    instead of copied, and reported as the payload path.
    """
    patch = source / PAYLOAD_PATCH_FILENAME
    files = sorted(path.relative_to(source) for path in source.rglob("*") if path.is_file() and path != patch)
    existing = [str(rel) for rel in files if (instance_dir / rel).exists()]
    if existing:
        raise FixtureError(f"fixture would replace instance files: {existing}")
    if patch.is_file():
        _patch_payload(patch, instance_dir / _PAYLOAD_PATH)
    for rel in files:
        target = instance_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / rel, target)
    return [_PAYLOAD_PATH, *files] if patch.is_file() else files


def _patch_payload(patch: Path, payload: Path) -> None:
    if not payload.is_file():
        raise FixtureError(f"{PAYLOAD_PATCH_FILENAME} needs a scaffolded {_PAYLOAD_PATH}")
    overrides = yaml.safe_load(patch.read_text(encoding="utf-8"))
    document = yaml.safe_load(payload.read_text(encoding="utf-8"))
    if not isinstance(overrides, dict) or not isinstance(document, dict):
        raise FixtureError(f"{PAYLOAD_PATCH_FILENAME} and {_PAYLOAD_PATH} must both be YAML mappings")
    merged = _merge(document, overrides)
    payload.write_text(yaml.safe_dump(merged, sort_keys=False, allow_unicode=True), encoding="utf-8")


def _merge(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """*base* with *overrides* applied: mappings merge key by key, anything else replaces."""
    merged = dict(base)
    for key, value in overrides.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _merge(current, value)
        else:
            merged[key] = value
    return merged


__all__ = ["PAYLOAD_PATCH_FILENAME", "FixtureError", "apply_fixture", "fixture_dir"]
