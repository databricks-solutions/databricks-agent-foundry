"""Wheel-install packaged-CLI smokes (issue #81, 68-A)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from agent_foundry.pytest_plugin import require_uv_install, uv_pip_install_cmd
from tests.standalone_repo import (
    PROJECT_ROOT,
    SYNTHETIC_INSTANCE_NAME,
    run_packaged_cli,
    seed_standalone_repo,
)


@pytest.mark.component
def test_wheel_dry_run_from_standalone_directory(tmp_path: Path) -> None:
    """Install the wheel into a fresh venv and run the packaged
    ``agent-foundry`` console script from a directory that contains only
    a synthetic ``instances/`` and ``conf/`` layout.  End-to-end proof
    that the framework is library-grade (issue #81).

    We avoid importing anything from the source tree inside the
    subprocess so the test actually exercises the packaged install.
    """
    if shutil.which("uv") is None:
        pytest.skip("uv is required for the wheel-install smoke test")

    venv_dir = tmp_path / "venv"
    downstream = tmp_path / "downstream-repo"
    seed_standalone_repo(downstream)

    subprocess.run(
        ["uv", "venv", str(venv_dir), "--python", sys.executable],
        check=True,
    )
    venv_python = venv_dir / "bin" / "python"
    venv_script = venv_dir / "bin" / "agent-foundry"

    install = subprocess.run(
        uv_pip_install_cmd(str(venv_python), str(PROJECT_ROOT)),
        capture_output=True,
        text=True,
    )
    require_uv_install(install, "uv pip install .")

    assert venv_script.exists(), (
        f"'agent-foundry' console script missing at {venv_script}; check [project.scripts] in pyproject.toml"
    )

    help_result = run_packaged_cli(venv_script, ["--help"], cwd=downstream, timeout=30)
    help_out = help_result.stdout + help_result.stderr
    assert help_result.returncode == 0, f"agent-foundry --help failed (rc={help_result.returncode}):\n{help_out}"
    assert "deploy" in help_out.lower(), "Expected deploy in --help. Got:\n" + help_out

    validate_result = run_packaged_cli(venv_script, ["validate"], cwd=downstream, timeout=60)
    validate_out = validate_result.stdout + validate_result.stderr
    if validate_result.returncode != 0:
        help_validate = run_packaged_cli(venv_script, ["validate", "--help"], cwd=downstream, timeout=30)
        help_validate_out = help_validate.stdout + help_validate.stderr
        assert help_validate.returncode == 0, (
            f"validate failed (rc={validate_result.returncode}):\n{validate_out}\n"
            f"and validate --help failed (rc={help_validate.returncode}):\n{help_validate_out}"
        )
        assert "validate" in help_validate_out.lower() or "prereq" in help_validate_out.lower()
    else:
        assert "PASS" in validate_out or "FAIL" in validate_out or "check" in validate_out.lower(), (
            "Expected validate to print check results. Got:\n" + validate_out
        )

    result = run_packaged_cli(
        venv_script,
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
    )

    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"agent-foundry deploy --dry-run --offline failed (rc={result.returncode}):\n{combined}"
    )
    assert "Deployment Summary" in combined, "Expected 'Deployment Summary' in CLI output. Got:\n" + combined

    destroy_result = run_packaged_cli(
        venv_script,
        [
            "destroy",
            "--instance",
            SYNTHETIC_INSTANCE_NAME,
            "--target",
            "dev",
            "--dry-run",
            "--offline",
        ],
        cwd=downstream,
    )
    destroy_out = destroy_result.stdout + destroy_result.stderr
    assert destroy_result.returncode == 0, (
        f"agent-foundry destroy --dry-run --offline failed (rc={destroy_result.returncode}):\n{destroy_out}"
    )

    start_app_result = subprocess.run(
        [
            str(venv_python),
            "-m",
            "bricks.app.scripts.start_app",
            "--no-ui",
            "--help",
        ],
        cwd=str(downstream),
        capture_output=True,
        text=True,
        timeout=60,
    )
    start_app_combined = start_app_result.stdout + start_app_result.stderr
    assert start_app_result.returncode == 0, (
        f"start_app --no-ui --help failed (rc={start_app_result.returncode}):\n{start_app_combined}"
    )
    assert "Start bundled agent backend + chat UI" in start_app_combined, (
        "Expected the start_app help banner. Got:\n" + start_app_combined
    )


@pytest.mark.component
def test_runtime_load_variables_explicit_project_root(tmp_path: Path) -> None:
    """Issue #143 wheel-install contract test.

    Installs the wheel WITH the ``[runtime]`` extra into a fresh venv,
    then exercises ``payloads.agent.runtime.runtime_config.load_variables()`` from a
    downstream-style CWD.
    """
    if shutil.which("uv") is None:
        pytest.skip("uv is required for the wheel-install contract test")

    venv_dir = tmp_path / "venv"
    downstream = tmp_path / "downstream-repo"
    seed_standalone_repo(downstream)

    subprocess.run(
        ["uv", "venv", str(venv_dir), "--python", sys.executable],
        check=True,
    )
    venv_python = venv_dir / "bin" / "python"

    install = subprocess.run(
        uv_pip_install_cmd(str(venv_python), f"{PROJECT_ROOT}[runtime]"),
        capture_output=True,
        text=True,
    )
    require_uv_install(install, "uv pip install .[runtime]")

    one_liner = (
        "import sys, json; "
        "from payloads.agent.runtime.runtime_config import load_variables; "
        "sys.stdout.write(json.dumps(load_variables()))"
    )

    positive = subprocess.run(
        [str(venv_python), "-c", one_liner],
        cwd=str(downstream),
        env={
            **os.environ,
            "AGENT_FOUNDRY_PROJECT_ROOT": str(downstream),
            "INSTANCE_DIR": f"instances/{SYNTHETIC_INSTANCE_NAME}",
            "DEPLOY_TARGET": "dev",
            "AGENT_CATALOG": "main",
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert positive.returncode == 0, (
        f"load_variables() failed with env set (rc={positive.returncode}):\n"
        f"stdout: {positive.stdout}\nstderr: {positive.stderr}"
    )
    payload = json.loads(positive.stdout)
    assert payload.get("app_name") == SYNTHETIC_INSTANCE_NAME, (
        f"Expected app_name from the seeded per-instance foundry.toml; got {payload!r}"
    )
    assert payload.get("agent_schema") == "main.smoke_test_app_agent", (
        f"Expected agent_schema from foundry.toml schema_prefix + AGENT_CATALOG; got {payload!r}"
    )

    negative_env = {k: v for k, v in os.environ.items() if k != "AGENT_FOUNDRY_PROJECT_ROOT"}
    negative_env["INSTANCE_DIR"] = f"instances/{SYNTHETIC_INSTANCE_NAME}"
    negative = subprocess.run(
        [str(venv_python), "-c", one_liner],
        cwd=str(downstream),
        env=negative_env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert negative.returncode != 0, (
        "load_variables() must raise when AGENT_FOUNDRY_PROJECT_ROOT is unset (no silent CWD fallback)."
    )
    assert "AGENT_FOUNDRY_PROJECT_ROOT" in negative.stderr, (
        f"Expected the missing-env error message to name AGENT_FOUNDRY_PROJECT_ROOT. Got stderr:\n{negative.stderr}"
    )
