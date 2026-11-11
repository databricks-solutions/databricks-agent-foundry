"""Declare and enforce payload deploy-time brick requirements (#454).

A payload lists bricks it needs in ``payload.yml``::

    payload:
      adapter: agent
      requires: [warehouse, tracing, observability]

Bricks such as tracing/observability call
:func:`payload_requires_brick` from ``applies_to`` instead of hard-coding
payload names.  :func:`enforce_payload_requirements` fails the deploy
when a required brick is ``--skip``'d on a full deploy, or cannot run.
"""

from __future__ import annotations

import importlib
from collections.abc import Mapping, Set
from pathlib import Path

from agent_foundry.deploy.payload_adapters import load_payload_config
from agent_foundry.loader import FoundryConfigError


def payload_requires_brick(instance_dir: Path | None, brick_name: str) -> bool:
    """True when the instance payload listed *brick_name* in ``requires``."""
    return brick_name in load_payload_config(instance_dir).requires


def enforce_payload_requirements(
    instance_dir: Path,
    variables: Mapping[str, str],
    *,
    skip_bricks: Set[str] | None = None,
    only_bricks: Set[str] | None = None,
) -> None:
    """Raise :class:`FoundryConfigError` when a declared requirement cannot run.

    ``--only`` is a partial deploy and does not have to satisfy every
    requirement.  A full deploy (or ``--skip`` of a required brick)
    must.
    """
    skip_bricks = skip_bricks or set()
    only_bricks = only_bricks or set()
    cfg = load_payload_config(instance_dir)

    for brick_name in cfg.requires:
        if only_bricks:
            continue
        if _token_skips(brick_name, skip_bricks):
            raise FoundryConfigError(
                f"payload {cfg.adapter!r} requires brick {brick_name!r}, "
                f"but it was skipped. Remove --skip {brick_name} or drop "
                f"{brick_name!r} from payload.requires."
            )
        unmet = _unmet_instance_dependency(brick_name, instance_dir, variables)
        if unmet:
            raise FoundryConfigError(unmet)


def _token_skips(brick_name: str, skip_bricks: Set[str]) -> bool:
    name = brick_name.lower()
    return any(token in name for token in skip_bricks)


def _unmet_instance_dependency(brick_name: str, instance_dir: Path, variables: Mapping[str, str]) -> str | None:
    if brick_name == "warehouse":
        return _unmet_warehouse(instance_dir, variables)
    if brick_name == "tracing":
        try:
            importlib.import_module("mlflow")
        except ImportError:
            return (
                "payload requires tracing but mlflow is not installed in this "
                "environment. Install agent-foundry[runtime] (or [eval]) "
                "or remove 'tracing' from payload.requires."
            )
        if not variables.get("tracing_experiment"):
            return "payload requires tracing but tracing_experiment is not configured for this instance."
    if brick_name == "observability" and not variables.get("tracing_experiment"):
        return "payload requires observability but tracing_experiment is not configured for this instance."
    return None


def _unmet_warehouse(instance_dir: Path, variables: Mapping[str, str]) -> str | None:
    """A warehouse resolves from ``warehouse_id`` (reuse mode, ``--vars``) or a dedicated ``warehouse/config.yml``."""
    if variables.get("warehouse_id") or (instance_dir / "warehouse" / "config.yml").exists():
        return None
    return (
        "payload requires a SQL warehouse but none is configured for this instance. "
        f"Run `agent-foundry scaffold warehouse --instance {instance_dir.name}` for a dedicated one, "
        'or set [warehouse] mode = "reuse" with warehouse_id in foundry.toml.'
    )
