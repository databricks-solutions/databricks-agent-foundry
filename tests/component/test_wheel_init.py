"""Packaged ``agent-foundry init`` smoke: blank repo plus every shipped example's offline tests."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement

from agent_foundry.init.examples_registry import list_variants
from agent_foundry.pytest_plugin import require_uv_install, uv_pip_install_cmd
from tests.standalone_repo import PROJECT_ROOT, packaged_cli_env, run_packaged_cli

_HOST = "https://wks.cloud.databricks.com"
_PROJECT = "smoke-init"
_OFFLINE_MARKERS = "not live and not component"


def _init_stdin() -> str:
    """Wizard answers for a blank GitHub repo (dev target only).

    Matches ``agent_foundry/tests/unit/init/test_cli_init.py`` happy-path order.
    ``--name`` still leaves the project-name prompt in place.
    """
    return (
        "\n".join(
            [
                "wizard-said",
                _HOST,
                "dev_cat",
                "",
                "n",
                "n",
                "git",
                "blank",
            ]
        )
        + "\n"
    )


@pytest.mark.component
def test_wheel_init_blank_github_repo(tmp_path: Path) -> None:
    """Install the wheel and run the real ``init`` console script.

    ``--no-lock`` / ``--no-sync`` keep this a packaging smoke: the
    generated pin is git+ssh, not the wheel under test.
    """
    if shutil.which("uv") is None:
        pytest.skip("uv is required for the wheel-install smoke test")

    venv_dir = tmp_path / "venv"
    outdir = tmp_path / "generated"
    outdir.mkdir()

    subprocess.run(
        ["uv", "venv", str(venv_dir), "--python", sys.executable],
        check=True,
    )
    venv_script = venv_dir / "bin" / "agent-foundry"
    venv_python = venv_dir / "bin" / "python"

    install = subprocess.run(
        uv_pip_install_cmd(str(venv_python), str(PROJECT_ROOT)),
        capture_output=True,
        text=True,
    )
    require_uv_install(install, "uv pip install .")
    assert venv_script.exists(), (
        f"'agent-foundry' console script missing at {venv_script}; check [project.scripts] in pyproject.toml"
    )

    result = run_packaged_cli(
        venv_script,
        [
            "init",
            "--outdir",
            str(outdir),
            "--name",
            _PROJECT,
            "--no-lock",
            "--no-sync",
            "--framework-ref",
            "smoke-test",
        ],
        cwd=outdir,
        timeout=120,
        input=_init_stdin(),
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, f"agent-foundry init failed (rc={result.returncode}):\n{combined}"

    generated = outdir / _PROJECT
    pyproject = generated / "pyproject.toml"
    assert pyproject.is_file(), f"missing pyproject.toml; output:\n{combined}"
    pyproject_text = pyproject.read_text()
    assert "agent-foundry" in pyproject_text
    assert "smoke-test" in pyproject_text
    assert (generated / "instances" / _PROJECT / "foundry.toml").is_file()
    assert (generated / ".github" / "workflows" / "pr-to-main.yml").is_file()
    assert not (generated / ".gitlab-ci.yml").exists()
    assert not (generated / "uv.lock").exists()
    assert "Done. Your repo is at" in combined
    assert not (outdir / "wizard-said").exists()


def _example_stdin(variant_id: str, project: str) -> str:
    return "\n".join([project, _HOST, "dev_cat", "", "n", "n", "git", variant_id]) + "\n"


def _framework_extras(pyproject: Path) -> str:
    """Extras the generated repo pins on ``agent-foundry`` (the consumer contract)."""
    dependencies = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["dependencies"]
    pin = next(Requirement(dep) for dep in dependencies if Requirement(dep).name == "agent-foundry")
    return ",".join(sorted(pin.extras))


@pytest.mark.component
@pytest.mark.parametrize("variant_id", [v.id for v in list_variants()])
def test_wheel_init_example_offline_tests_pass(tmp_path: Path, variant_id: str) -> None:
    """Init each shipped example from the wheel, then run its offline tests there.

    The generated pin is a git SHA, so the candidate wheel (with the
    example's pinned extras) is installed explicitly before pytest runs.
    """
    if shutil.which("uv") is None:
        pytest.skip("uv is required for the wheel-install smoke test")

    project = f"{variant_id.replace('_', '-')}-smoke"
    venv_dir = tmp_path / "venv"
    outdir = tmp_path / "generated"
    outdir.mkdir()
    subprocess.run(["uv", "venv", str(venv_dir), "--python", sys.executable], check=True)
    venv_python = venv_dir / "bin" / "python"
    install = subprocess.run(
        uv_pip_install_cmd(str(venv_python), str(PROJECT_ROOT)),
        capture_output=True,
        text=True,
    )
    require_uv_install(install, "uv pip install .")

    result = run_packaged_cli(
        venv_dir / "bin" / "agent-foundry",
        ["init", "--outdir", str(outdir), "--name", project, "--no-lock", "--no-sync", "--framework-ref", "smoke-test"],
        cwd=outdir,
        timeout=120,
        input=_example_stdin(variant_id, project),
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, f"init failed (rc={result.returncode}):\n{combined}"
    generated = outdir / project
    unit_tests = generated / "instances" / project / "tests" / "unit"
    assert unit_tests.is_dir(), f"example unit tests not shipped:\n{combined}"

    extras = _framework_extras(generated / "pyproject.toml")
    install = subprocess.run(
        uv_pip_install_cmd(str(venv_python), f"{PROJECT_ROOT}[{extras}]"),
        capture_output=True,
        text=True,
    )
    require_uv_install(install, f"uv pip install .[{extras}]")

    tests = subprocess.run(
        [str(venv_python), "-m", "pytest", "-q", "-p", "no:cacheprovider", "-m", _OFFLINE_MARKERS, str(unit_tests)],
        cwd=generated,
        env=packaged_cli_env(),
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert tests.returncode == 0, f"{variant_id} offline tests failed:\n{tests.stdout}\n{tests.stderr}"
