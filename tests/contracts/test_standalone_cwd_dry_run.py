"""Cheap console-script smoke: existing venv, ``--offline``, no wheel build."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.standalone_repo import (
    SYNTHETIC_INSTANCE_NAME,
    packaged_cli_env,
    run_packaged_cli,
    seed_standalone_repo,
)


def test_standalone_cwd_dry_run(tmp_path: Path) -> None:
    """Exercise the CLI from a tmp cwd that has no framework source tree.

    Uses the current venv's ``agent-foundry`` console script -- so the
    CLI's path-resolution code (``Path.cwd()`` defaults, no
    ``Path(__file__)`` walks) is what resolves ``foundry.toml`` and
    ``instances/``.
    ``--offline`` keeps the subprocess off credential discovery.
    Network-free, fast (< 2s), always on.
    """
    downstream = tmp_path / "downstream-repo"
    seed_standalone_repo(downstream)

    script = Path(sys.executable).parent / "agent-foundry"
    if not script.exists():
        pytest.skip(
            f"agent-foundry console script not in {script.parent}; "
            "re-install the project with `pip install -e .` or `uv pip install -e .`"
        )

    env = packaged_cli_env()
    assert "AGENT_FOUNDRY_SKIP_DEFAULT_WORKSPACE_CLIENT" not in env
    result = run_packaged_cli(
        script,
        [
            "deploy",
            "--instance",
            SYNTHETIC_INSTANCE_NAME,
            "--target",
            "dev",
            "--dry-run",
            "--offline",
        ],
        cwd=downstream,
        timeout=30,
    )

    combined = result.stdout + result.stderr
    assert result.returncode == 0, f"agent-foundry deploy --dry-run failed (rc={result.returncode}):\n{combined}"
    assert "Deployment Summary" in combined, "Expected 'Deployment Summary' in CLI output. Got:\n" + combined
