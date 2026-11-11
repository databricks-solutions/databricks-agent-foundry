"""What a ``tests/live/`` check points at: one CLI-deployed instance in a real workspace."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Any

from agent_foundry import env
from agent_foundry.sql_statements import quote_identifier
from agent_foundry.testing.workspace import WorkspaceBinding, WorkspaceConfigError, load_workspace_binding

_TRUTHY = frozenset({"1", "true", "yes"})
_SQL_POLL_SECONDS = 2.0
_LIVE_DIR = ("tests", "live")


def is_live_path(path: PurePath) -> bool:
    """True for paths under a ``tests/live/`` folder (any depth, any owner)."""
    parts = path.parts
    return any(pair == _LIVE_DIR for pair in zip(parts, parts[1:]))


@dataclass(frozen=True)
class LiveTarget:
    """Workspace binding plus the generated project whose deployment is checked."""

    binding: WorkspaceBinding
    project_root: Path
    instance: str

    @property
    def instance_dir(self) -> Path:
        return self.project_root / "instances" / self.instance


def live_required() -> bool:
    """True in canary jobs, where a missing binding must fail instead of skip."""
    return os.environ.get(env.AGENT_FOUNDRY_REQUIRE_LIVE.name, "").strip().lower() in _TRUTHY


def load_live_target() -> LiveTarget:
    """Resolve the binding and the single instance under ``AGENT_FOUNDRY_PROJECT_ROOT`` (default: cwd)."""
    binding = load_workspace_binding()
    raw_root = os.environ.get(env.AGENT_FOUNDRY_PROJECT_ROOT.name, "").strip()
    project_root = Path(raw_root).resolve() if raw_root else Path.cwd()
    return LiveTarget(binding=binding, project_root=project_root, instance=_single_instance(project_root))


def load_live_variables(target: LiveTarget, ws: Any) -> dict[str, str]:
    """Variable bag plus published brick outputs (``warehouse_id``, ...) for the deployed instance.

    Outputs are re-read with *ws* because some bricks (Genie, MCP) keep
    state in the workspace only.
    """
    from agent_foundry.bricks import load_brick_variables

    return load_brick_variables(target.project_root, target.binding.target, target.instance_dir, ws=ws)


def published_outputs(variables: dict[str, str], base: str) -> dict[str, str]:
    """Nested-layout outputs ``<base>__<sub>`` as ``{sub: value}`` (e.g. ``genie_space_id``)."""
    prefix = f"{base}__"
    return {key.removeprefix(prefix): value for key, value in variables.items() if key.startswith(prefix) and value}


def run_sql(
    ws: Any,
    warehouse_id: str,
    statement: str,
    *,
    parameters: dict[str, str] | None = None,
    timeout_seconds: float = 300,
) -> list[list[Any]]:
    """Run *statement* (``:name`` markers bound from *parameters*) and return its rows.

    Raises on failure or timeout. Polls past the API's 50s inline wait so
    a cold serverless start does not read as a failed check.
    """
    from databricks.sdk.service.sql import StatementParameterListItem, StatementState

    response = ws.statement_execution.execute_statement(
        statement=statement,
        warehouse_id=warehouse_id,
        parameters=[StatementParameterListItem(name=k, value=v) for k, v in (parameters or {}).items()],
        wait_timeout="50s",
    )
    deadline = time.monotonic() + timeout_seconds
    while response.status.state in (StatementState.PENDING, StatementState.RUNNING):
        if time.monotonic() > deadline:
            raise TimeoutError(f"statement {response.statement_id} still running after {timeout_seconds}s")
        time.sleep(_SQL_POLL_SECONDS)
        response = ws.statement_execution.get_statement(response.statement_id)
    if response.status.state != StatementState.SUCCEEDED:
        error = response.status.error.message if response.status.error else response.status.state
        raise RuntimeError(f"SQL failed ({error}): {statement}")
    result = response.result
    return list(result.data_array or []) if result is not None else []


def _single_instance(project_root: Path) -> str:
    instances_dir = project_root / "instances"
    names = sorted(p.name for p in instances_dir.iterdir() if p.is_dir()) if instances_dir.is_dir() else []
    if len(names) != 1:
        raise WorkspaceConfigError(
            f"live checks need exactly one instance under {instances_dir}, found {names}; "
            f"set {env.AGENT_FOUNDRY_PROJECT_ROOT.name} to the generated project"
        )
    return names[0]


__all__ = [
    "LiveTarget",
    "is_live_path",
    "live_required",
    "load_live_target",
    "load_live_variables",
    "published_outputs",
    "quote_identifier",
    "run_sql",
]
