"""Agent payload runtime-spec resolution (issue #337/#338).

Resolves the agent payload's ``app.yaml`` (brick default merged with the
instance override, ``${var.*}`` substituted) into the neutral
:class:`~agent_foundry.deploy.app_host.AppPayloadSpec` the App host
consumes.  This is the runtime-code half of the App-host seam: reading
package data + substitution lives with the payload; the engine consumes
the resolved spec.

Relocated from ``bricks/app/scripts/deploy_agent_app.py``.
"""

from __future__ import annotations

from collections.abc import Mapping
from importlib import resources as _resources
from pathlib import Path
from typing import Any

import yaml

from agent_foundry.deploy.app_host import AppPayloadSpec
from payloads.agent.contracts import AgentPayloadSpec
from payloads.agent.deploy.config import env_overrides_from_spec, resolve_app_config_dir


def _apply_env_overrides(env: list[dict[str, Any]], overrides: Mapping[str, str]) -> list[dict[str, Any]]:
    """Merge *overrides* into *env* by name (override wins, extras appended)."""
    by_name = {e["name"]: e for e in env}
    for name, value in overrides.items():
        by_name[name] = {"name": name, "value": value}
    return list(by_name.values())


def _merge_app_yaml(brick_data: dict[str, Any], instance_data: dict[str, Any]) -> dict[str, Any]:
    """Merge instance app.yaml overrides into brick default.

    ``command`` is replaced if present in instance.
    ``env`` entries are merged by ``name``: instance entries override
    brick entries with the same name; extras are appended.
    """
    merged = dict(brick_data)
    if "command" in instance_data:
        merged["command"] = instance_data["command"]
    if "env" in instance_data:
        base_env = {e["name"]: e for e in merged.get("env", [])}
        for entry in instance_data["env"]:
            base_env[entry["name"]] = entry
        merged["env"] = list(base_env.values())
    return merged


def _load_brick_app_yaml_text() -> str | None:
    """Read the brick-default ``app.yaml`` from package data.

    Returns ``None`` if the packaged resource is missing (e.g. during
    tests that exercise an isolated scenario).  Replaces the previous
    ``Path(__file__).parent.parent / \"app.yaml\"`` read that broke on
    wheel installs (issue #81, 68-A).
    """
    try:
        ref = _resources.files("bricks.app").joinpath("app.yaml")
        return ref.read_text(encoding="utf-8")
    except (ModuleNotFoundError, FileNotFoundError):
        return None


def _resolve_app_payload(
    instance_dir: Path | None,
    variables: dict[str, str],
    *,
    requirements_extras: tuple[str, ...],
    spec: AgentPayloadSpec,
) -> AppPayloadSpec:
    """Resolve the agent payload's runtime contract (issue #337/#338).

    Loads the brick-default ``app.yaml``, merges instance overrides,
    substitutes ``${var.*}`` placeholders, and packages the result --
    ``command`` + ``env`` (``valueFrom`` entries preserved) -- into a
    typed :class:`AppPayloadSpec`.  This is the agent-specific half of
    the seam: reading package data + substitution stays in the payload;
    the engine consumes the resolved spec.

    The deploy-time runtime toggles (``ENABLE_MCP_SERVER`` /
    ``ENABLE_A2A`` / ``ENABLE_FILE_UPLOAD`` / ``ENABLE_CHARTS`` /
    ``ENABLE_FEEDBACK`` / ``FILE_UPLOAD_MAX_MB``) are derived from the
    agent ``spec`` (the single source) and override any same-named env
    entry; the brick-default ``app.yaml`` carries infra env only.
    """
    data: dict[str, Any] = {}
    brick_yaml_text = _load_brick_app_yaml_text()
    if brick_yaml_text is not None:
        data = yaml.safe_load(brick_yaml_text) or {}

    if instance_dir:
        instance_yaml_path = resolve_app_config_dir(instance_dir) / "app.yaml"
        if instance_yaml_path.exists():
            overrides = yaml.safe_load(instance_yaml_path.read_text(encoding="utf-8")) or {}
            data = _merge_app_yaml(data, overrides)

    if not data:
        return AppPayloadSpec(
            env=[{"name": name, "value": value} for name, value in env_overrides_from_spec(spec).items()],
            requirements_extras=requirements_extras,
        )

    from agent_foundry.variables import substitute_obj

    # ``INSTANCE_DIR`` in app.yaml must point at the on-disk instance
    # folder, which staging copies to ``instances/<instance_dir.name>``
    # (see agent_foundry.deploy.staging) and which the runtime + state
    # paths key off. Expose the real directory name so the template does
    # not depend on ``app_name`` -- those diverge whenever ``app_name`` is
    # overridden (e.g. the 30-char Databricks Apps limit), which would
    # otherwise resolve INSTANCE_DIR to a non-existent path and silently
    # disable skills + MCP at runtime.
    if instance_dir is not None:
        variables = {**variables, "instance_dir_name": instance_dir.name}

    resolved: dict[str, Any] = substitute_obj(data, variables, strict=False)
    env = _apply_env_overrides(
        resolved.get("env", []),
        env_overrides_from_spec(spec),
    )
    return AppPayloadSpec(
        command=resolved.get("command", []),
        env=env,
        requirements_extras=requirements_extras,
    )
