"""Stable (or optionally unique) identity for upstream acceptance deploys."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from agent_foundry.testing.scenarios import Scenario

_SAFE_RUN = re.compile(r"[^a-z0-9]+")
_APP_NAME_MAX = 26
_RESOURCE_NAME_MAX = 63


class IdentityError(ValueError):
    """Run identity cannot be turned into a legal Databricks App name."""


@dataclass(frozen=True)
class RunIdentity:
    """Literal names written into the generated instance foundry.toml."""

    project_name: str
    schema_prefix: str
    app_name: str
    run_token: str


def make_run_identity(scenario: Scenario, run_id: str | None = None) -> RunIdentity:
    """Build a short Apps-legal identity for one harness run.

    Default token is ``canary`` so CI rebuilds the same App/schema on
    the next Framework acceptance run. Pass a unique *run_id* only when
    two deploys must coexist.
    """
    raw = run_id or os.environ.get("AGENT_FOUNDRY_RUN_ID") or "canary"
    token = _sanitize_run_token(raw)
    code = _scenario_code(scenario.id)
    project = f"af-{code}-{token}"
    if len(project) > _APP_NAME_MAX:
        raise IdentityError(f"project name {project!r} exceeds {_APP_NAME_MAX} chars")
    resource = f"dev-{project}-app"
    if len(resource) > _RESOURCE_NAME_MAX:
        raise IdentityError(f"app resource name {resource!r} exceeds {_RESOURCE_NAME_MAX} chars")
    return RunIdentity(
        project_name=project,
        schema_prefix=project.replace("-", "_"),
        app_name=project,
        run_token=token,
    )


def _sanitize_run_token(raw: str) -> str:
    cleaned = _SAFE_RUN.sub("", raw.lower())
    if not cleaned:
        raise IdentityError("run id is empty after sanitizing; set AGENT_FOUNDRY_RUN_ID")
    return cleaned[-6:]


def _scenario_code(scenario_id: str) -> str:
    if scenario_id == "rest_mcp_proxy":
        return "rm"
    if scenario_id == "portfolio_assistant":
        return "pa"
    letters = _SAFE_RUN.sub("", scenario_id.lower())
    if len(letters) < 2:
        raise IdentityError(f"cannot derive a 2-char code from scenario id {scenario_id!r}")
    return letters[:2]


__all__ = ["IdentityError", "RunIdentity", "make_run_identity"]
