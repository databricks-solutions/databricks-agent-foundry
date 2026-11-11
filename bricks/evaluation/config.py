"""Evaluation config loading shared by ``run_evaluation`` and ``register_monitors``."""

from __future__ import annotations

from importlib import resources as _resources
from pathlib import Path
from typing import Any

import yaml

from agent_foundry.variables import substitute


def _load_default_eval_config_text() -> str:
    """Read the brick-default ``eval_config.yml`` from package data, so it
    resolves when the framework is installed as a wheel (issue #81, 68-A)."""
    ref = _resources.files("bricks.evaluation").joinpath(
        "conf",
        "eval_config.yml",
    )
    return ref.read_text(encoding="utf-8")


def load_eval_config(
    instance_dir: Path | None,
    variables: dict[str, str],
) -> dict[str, Any]:
    """Load evaluation config with instance override support.

    Precedence: instance evaluation/eval_config.yml > brick default.
    Variable substitution is applied to string values.
    """
    if instance_dir is not None:
        override = instance_dir / "evaluation" / "eval_config.yml"
        if override.exists():
            raw = override.read_text(encoding="utf-8")
            substituted = substitute(raw, variables, strict=False)
            return yaml.safe_load(substituted) or {}

    raw = _load_default_eval_config_text()
    substituted = substitute(raw, variables, strict=False)
    return yaml.safe_load(substituted) or {}


__all__ = ["load_eval_config"]
