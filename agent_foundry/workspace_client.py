"""
Shared WorkspaceClient Factory
================================
Single place to create a :class:`databricks.sdk.WorkspaceClient` that
respects the ``profile`` or ``host`` from the target configuration.

Every deploy script should use this instead of calling
``WorkspaceClient()`` directly so the CLI profile or workspace host
from ``foundry.toml`` is always honoured.

Libraries that build their own ``WorkspaceClient`` internally (MLflow)
cannot be handed ours; :func:`export_mlflow_auth_env` pins the same
resolved auth for them via the environment.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from agent_foundry import env
from agent_foundry.telemetry import product_kwargs

SKIP_DEFAULT_WORKSPACE_CLIENT_ENV = "AGENT_FOUNDRY_SKIP_DEFAULT_WORKSPACE_CLIENT"


def skip_default_workspace_client() -> bool:
    """True when unit tests / CI forbid constructing a default SDK client."""
    return os.environ.get(SKIP_DEFAULT_WORKSPACE_CLIENT_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def prefer_workspace_client(
    variables: dict[str, str] | None = None,
    *,
    ws: Any | None = None,
    profile: str | None = None,
) -> Any | None:
    """Return an injected client, or construct one for standalone CLI use.

    Never constructs when the unit-test skip env is set and *ws* is
    ``None``. Brick scripts must pass the orchestrator's ``ctx.ws`` so
    a deploy/destroy run uses one client; the factory is only the
    last-resort path for ``python bricks/.../scripts/*.py``.
    """
    if ws is not None:
        return ws
    if skip_default_workspace_client():
        return None
    return get_workspace_client(variables, profile=profile)


def get_workspace_client(
    variables: dict[str, str] | None = None,
    *,
    profile: str | None = None,
) -> Any:
    """Create a ``WorkspaceClient`` from the target's profile or host.

    Resolution order:
    1. Explicit *profile* keyword argument.
    2. ``variables["profile"]`` (the ``foundry.toml`` target block).
    3. ``variables["host"]`` — ``agent-foundry init`` writes host without a
       profile; passing it avoids falling through to ``[DEFAULT]``.
    4. SDK default (env vars, ``~/.databrickscfg`` default section, etc.).

    Profile wins over host when both are set (several CLI profiles can
    share a host). Host alone is not a credential: OAuth still needs
    ``databricks auth login --host <host>``; PAT auth uses host + token.
    """
    if skip_default_workspace_client():
        raise RuntimeError(
            f"{SKIP_DEFAULT_WORKSPACE_CLIENT_ENV} is set; refusing to construct "
            "WorkspaceClient (SDK credential discovery can hang for minutes)."
        )

    from databricks.sdk import WorkspaceClient

    extra: Any = product_kwargs()
    vars_ = variables or {}
    resolved_profile = profile or vars_.get("profile") or None
    if resolved_profile:
        return WorkspaceClient(profile=resolved_profile, **extra)
    host = vars_.get("host") or None
    if host:
        return WorkspaceClient(host=host, **extra)
    return WorkspaceClient(**extra)


def export_mlflow_auth_env(variables: dict[str, str]) -> None:
    """Pin Databricks auth for MLflow's *own* SDK client.

    MLflow builds a ``WorkspaceClient`` from ambient default auth rather
    than the one agent-foundry resolves from ``variables``, and setting
    the tracking URI to ``databricks://<profile>`` does not reach that
    client.  When ``~/.databrickscfg`` holds several profiles that share
    a host, the default lookup is ambiguous and the call 401s; with an
    unambiguous ``[DEFAULT]`` it silently reads the wrong workspace.

    Call this before any ``mlflow.*`` call in a deploy/eval script.
    ``setdefault`` so an explicit caller env still wins.

    Seen live three times before it was shared: ``ensure_experiment``,
    ``destroy``, and the evaluation brick's trace fetch (#421).
    """
    profile = variables.get("profile", "")
    if profile:
        os.environ.setdefault(env.DATABRICKS_CONFIG_PROFILE.name, profile)
    host = variables.get("host", "")
    if host:
        os.environ.setdefault(env.DATABRICKS_HOST.name, host)


def load_target_auth_variables(project_root: Path, target: str) -> dict[str, str]:
    """Resolve ``profile`` / ``host`` for a named target.

    Reads the ``foundry.toml`` target block so callers that only have a
    repo root (``agent-foundry test``, ``validate --workspace``)
    authenticate against the declared profile instead of ``[DEFAULT]``.
    Returns ``{}`` when the file or target is missing or unreadable.
    """
    from agent_foundry.loader import load_repo_config

    try:
        tgt = load_repo_config(project_root).targets.get(target)
    except Exception:
        return {}
    if tgt is None:
        return {}
    variables: dict[str, str] = {}
    if tgt.profile:
        variables["profile"] = tgt.profile
    if tgt.host:
        variables["host"] = tgt.host
    return variables
