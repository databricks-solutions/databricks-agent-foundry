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


def get_experiment_name(
    config: dict[str, Any],
    variables: dict[str, str] | None = None,
) -> str:
    """Return the experiment name (single experiment for all traces and evaluation).

    Falls back to ``variables["tracing_experiment"]`` (computed by InstanceConfig)
    when the eval config doesn't specify an explicit experiment name.
    """
    name: str | None = config.get("experiment_name") or config.get("production_experiment")
    if name:
        return name
    if variables and variables.get("tracing_experiment"):
        return variables["tracing_experiment"]
    return "agent_evaluation"


__all__ = ["get_experiment_name", "load_eval_config"]
