"""Entry-point extensibility end-to-end test (issue #82, 68-B).

Proves that a third-party distribution declaring a brick under the
``agent_foundry.bricks`` entry-point group is discovered by
``iter_bricks()`` without any change to the framework.

Marked ``@pytest.mark.component`` because it builds a wheel and
installs packages into a fresh venv via ``uv`` (no Databricks).  Skipped
when ``uv`` is not on PATH; network failure skips locally and fails in CI.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from agent_foundry.pytest_plugin import require_uv_install, uv_pip_install_cmd

PROJECT_ROOT = Path(__file__).resolve().parents[2]


FAKE_BRICK_PYPROJECT = """\
[project]
name = "fake-brick-dist"
version = "0.0.1"
requires-python = ">=3.11"
dependencies = ["agent-foundry", "pydantic>=2"]

[project.entry-points."agent_foundry.bricks"]
hello_brick = "fake_brick_dist:HelloBrick"

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
include = ["fake_brick_dist*"]
"""

FAKE_BRICK_MODULE = (
    textwrap.dedent(
        """
    from agent_foundry import Brick, BrickResult, DeployContext


    class HelloBrick:
        name = "hello_brick"
        deploy_order = 999
        state_schema = None

        def applies_to(self, ctx: DeployContext) -> bool:
            return False

        def deploy(self, ctx: DeployContext) -> list[BrickResult]:
            return [BrickResult(name="hello", status="ok")]


    # Runtime protocol check at import time.
    _: Brick = HelloBrick()
    """
    ).strip()
    + "\n"
)


def _materialise_fake_dist(root: Path) -> Path:
    """Write the fake brick distribution to *root* and return its path."""
    dist = root / "fake-brick-dist"
    pkg = dist / "fake_brick_dist"
    pkg.mkdir(parents=True)
    (dist / "pyproject.toml").write_text(FAKE_BRICK_PYPROJECT)
    (pkg / "__init__.py").write_text(FAKE_BRICK_MODULE)
    return dist


@pytest.mark.component
def test_fake_brick_registered_via_entry_point(tmp_path: Path) -> None:
    """Install the fake dist into a fresh venv and assert iter_bricks()
    finds ``hello_brick``."""
    if shutil.which("uv") is None:
        pytest.skip("uv is required for the entry-point extensibility test")

    venv_dir = tmp_path / "venv"
    dist_dir = _materialise_fake_dist(tmp_path)

    subprocess.run(
        ["uv", "venv", str(venv_dir), "--python", sys.executable],
        check=True,
    )
    venv_python = venv_dir / "bin" / "python"

    # Install the framework + the fake brick into the fresh venv.
    # ``agent-foundry`` resolves locally; the fake brick's sole
    # non-framework dep is pydantic which the framework already pulls
    # in.
    install = subprocess.run(
        uv_pip_install_cmd(str(venv_python), str(PROJECT_ROOT), str(dist_dir)),
        capture_output=True,
        text=True,
    )
    require_uv_install(install, "uv pip install (fake brick entry point)")

    probe = subprocess.run(
        [
            str(venv_python),
            "-c",
            "from agent_foundry import iter_bricks; names = [b.name for b in iter_bricks()]; print('\\n'.join(names))",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    names = probe.stdout.strip().splitlines()
    assert "hello_brick" in names, (
        "HelloBrick from the fake dist was not discovered via the "
        f"agent_foundry.bricks entry point. Registered bricks: {names!r}"
    )
    assert "sql_function" in names, (
        f"Built-in bricks should remain registered alongside third-party ones. Got: {names!r}"
    )
