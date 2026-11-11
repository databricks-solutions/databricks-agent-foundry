"""Standalone brick CLIs fold recorded deploy outputs into their variables (#598).

``agent-foundry deploy`` preloads every brick's state outputs
(``warehouse_id``, ``genie_space_id__<space>``, ...).  A brick run through
its own CLI builds its workspace client from the variable bag, so it must
call :func:`agent_foundry.bricks.fill_state_outputs` once that client
exists; otherwise a brick whose upstream was deployed earlier sees no
``warehouse_id`` (#416).

* A static scan: every brick / payload script that loads variables and
  builds a workspace client also calls ``fill_state_outputs``.
* End-to-end checks of two CLI shapes (genie: module ``_get_client``;
  job: ``prefer_workspace_client``) against a seeded fake workspace.
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path
from typing import Any

import pytest

from agent_foundry.state import save_state
from agent_foundry.testing.fakes import FakeWorkspaceClient

REPO_ROOT = Path(__file__).resolve().parents[2]

SCAN_ROOTS: tuple[Path, ...] = (REPO_ROOT / "bricks", REPO_ROOT / "payloads")

EXCLUDED_DIRS = frozenset({"tests", "__pycache__", "node_modules", "_scaffold"})

CLIENT_FACTORIES = frozenset({"prefer_workspace_client", "get_workspace_client"})

INSTANCE = "demo"
TARGET = "dev"
RECORDED_WAREHOUSE_ID = "wh-from-state"


def _called_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            names.add(func.id)
        elif isinstance(func, ast.Attribute):
            names.add(func.attr)
    return names


def _is_construct_true(keyword: ast.keyword) -> bool:
    value = keyword.value
    return keyword.arg == "construct" and isinstance(value, ast.Constant) and value.value is True


def _constructs_client(tree: ast.AST) -> bool:
    """A client factory call, or a script-local helper called with ``construct=True``."""
    if CLIENT_FACTORIES & _called_names(tree):
        return True
    return any(
        isinstance(node, ast.Call) and any(_is_construct_true(kw) for kw in node.keywords) for node in ast.walk(tree)
    )


def needs_state_fill(source: str) -> bool:
    """Whether *source* loads brick variables and builds a client but never fills state."""
    tree = ast.parse(source)
    called = _called_names(tree)
    return "load_brick_variables" in called and _constructs_client(tree) and "fill_state_outputs" not in called


def _cli_modules() -> list[Path]:
    modules = [
        path
        for root in SCAN_ROOTS
        for path in root.rglob("*.py")
        if EXCLUDED_DIRS.isdisjoint(path.relative_to(REPO_ROOT).parts)
        and "load_brick_variables(" in path.read_text(encoding="utf-8")
    ]
    return sorted(modules)


def test_cli_modules_discovered() -> None:
    assert _cli_modules(), "No brick CLI modules calling load_brick_variables were found."


@pytest.mark.parametrize("module", _cli_modules(), ids=lambda p: p.relative_to(REPO_ROOT).as_posix())
def test_cli_folds_state_outputs_into_variables(module: Path) -> None:
    assert not needs_state_fill(module.read_text(encoding="utf-8")), (
        f"{module.relative_to(REPO_ROOT).as_posix()} builds a workspace client but never calls "
        "fill_state_outputs(variables, instance_dir, target, ws): outputs recorded by earlier "
        "deploys (warehouse_id, ...) will be missing from its variables (#598)."
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("v = load_brick_variables(r, t, d)\nws = _get_client(v, construct=True)", True),
        ("v = load_brick_variables(r, t, d)\nws = prefer_workspace_client(v)", True),
        (
            "v = load_brick_variables(r, t, d)\nws = prefer_workspace_client(v)\nv = fill_state_outputs(v, d, t, ws)",
            False,
        ),
        ("v = load_brick_variables(r, t, d)", False),
        ("v = load_brick_variables(r, t, d)\nws = _get_client(v, construct=False)", False),
    ],
)
def test_detector(source: str, expected: bool) -> None:
    assert needs_state_fill(source) is expected


def _write_repo(root: Path, sub_dir: str) -> Path:
    """A one-instance repo with no pinned warehouse and one sub-instance under *sub_dir*."""
    (root / "foundry.toml").write_text(
        textwrap.dedent(
            f"""
            schema_version = 2

            [targets.{TARGET}]
            host = "https://dev.cloud.databricks.com"
            catalog = "main"
            profile = "dev-profile"

            [[instances]]
            path = "instances/{INSTANCE}"
            """
        ).lstrip(),
        encoding="utf-8",
    )
    instance_dir = root / "instances" / INSTANCE
    (instance_dir / sub_dir / "one").mkdir(parents=True)
    (instance_dir / "foundry.toml").write_text(
        f'name = "{INSTANCE}"\nschema_prefix = "{INSTANCE}_app"\n',
        encoding="utf-8",
    )
    return instance_dir


def _workspace_with_recorded_warehouse(instance_dir: Path) -> FakeWorkspaceClient:
    (instance_dir / "warehouse").mkdir(exist_ok=True)
    ws = FakeWorkspaceClient()
    save_state(ws, INSTANCE, "warehouse", INSTANCE, TARGET, {"warehouse_id": RECORDED_WAREHOUSE_ID, "name": "wh"})
    return ws


class _DeployRecorder:
    """Stands in for a brick script's ``deploy``; keeps the variables it was given."""

    def __init__(self) -> None:
        self.variables: dict[str, str] = {}

    def __call__(self, _sub_dir: Path, _target: str, variables: dict[str, str], *_args: Any, **_kwargs: Any) -> int:
        self.variables = dict(variables)
        return 0


def test_genie_cli_sees_recorded_warehouse_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from bricks.genie.scripts import deploy as genie_deploy

    ws = _workspace_with_recorded_warehouse(_write_repo(tmp_path, "genie_spaces"))
    recorder = _DeployRecorder()
    monkeypatch.setattr(genie_deploy, "_get_client", lambda *_a, **_k: ws)
    monkeypatch.setattr(genie_deploy, "deploy", recorder)

    rc = genie_deploy.main(["--instance", INSTANCE, "--target", TARGET, "--project-root", str(tmp_path)])

    assert rc == 0
    assert recorder.variables["warehouse_id"] == RECORDED_WAREHOUSE_ID


def test_job_cli_resolves_the_owning_instance_and_sees_recorded_warehouse_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agent_foundry.workspace_client as workspace_client
    from bricks.job.scripts import deploy_job

    ws = _workspace_with_recorded_warehouse(_write_repo(tmp_path, "jobs"))
    recorder = _DeployRecorder()
    monkeypatch.setattr(workspace_client, "prefer_workspace_client", lambda *_a, **_k: ws)
    monkeypatch.setattr(deploy_job, "deploy", recorder)

    rc = deploy_job.main(["--instance", f"{INSTANCE}/jobs/one", "--target", TARGET, "--project-root", str(tmp_path)])

    assert rc == 0
    assert recorder.variables["warehouse_id"] == RECORDED_WAREHOUSE_ID
