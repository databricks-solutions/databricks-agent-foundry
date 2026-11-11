"""Payload-adapter entry-point extensibility end-to-end test (issue #368).

Proves that a third-party distribution declaring a payload adapter under
the ``agent_foundry.payload_adapters`` entry-point group is discovered by
``iter_payload_adapters()`` -- and that its scaffold verbs show up in the
composed scaffold registry -- without any change to the framework.

Marked ``@pytest.mark.component`` because it builds and installs
packages into a fresh venv via ``uv`` (no Databricks).  Skipped when
``uv`` is not on PATH; network failure skips locally and fails in CI.
Mirrors ``tests/component/test_fake_brick_entry_point.py``.
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


FAKE_PAYLOAD_PYPROJECT = """\
[project]
name = "fake-payload-dist"
version = "0.0.1"
requires-python = ">=3.11"
dependencies = ["agent-foundry"]

[project.entry-points."agent_foundry.payload_adapters"]
fake_payload = "fake_payload_dist:FakePayloadAdapter"

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
include = ["fake_payload_dist*"]
"""

FAKE_PAYLOAD_MODULE = (
    textwrap.dedent(
        '''
    from pathlib import Path

    from agent_foundry.deploy.payload_adapters import AppPayloadAdapter
    from agent_foundry.scaffold_spec import ScaffoldDecl, ScaffoldSource


    class FakePayloadAdapter:
        """A minimal third-party payload adapter for the discovery test."""

        name = "fake_payload"

        def applies_to(self, instance_dir, config):
            return False

        def build_payload_spec(self, instance_dir, variables, spec):
            raise NotImplementedError

        def build_infra(self, instance_dir, variables, *, target):
            raise NotImplementedError

        def after_reconcile(self, ws, app_name, variables, *, dry_run):
            return None

        def provision(self, ctx):
            return 0

        def plan_destroy(self, ctx):
            return []

        def destroy(self, ctx):
            return []

        def load_state_outputs(self, ctx):
            return {}

        def scaffolds(self):
            return (
                ScaffoldDecl(
                    name="fake_payload",
                    group="fake payload",
                    display="Fake Payload",
                    summary="A fake payload scaffold.",
                    sources=(
                        ScaffoldSource(Path(__file__), "payloads/fake/payload.yml", "file"),
                    ),
                ),
            )


    # Runtime protocol check at import time.
    _: AppPayloadAdapter = FakePayloadAdapter()
    '''
    ).strip()
    + "\n"
)


def _materialise_fake_dist(root: Path) -> Path:
    """Write the fake payload distribution to *root* and return its path."""
    dist = root / "fake-payload-dist"
    pkg = dist / "fake_payload_dist"
    pkg.mkdir(parents=True)
    (dist / "pyproject.toml").write_text(FAKE_PAYLOAD_PYPROJECT)
    (pkg / "__init__.py").write_text(FAKE_PAYLOAD_MODULE)
    return dist


_PROBE = (
    "from agent_foundry.deploy.payload_adapters import iter_payload_adapters; "
    "from agent_foundry.scaffold import build_scaffold_registry; "
    "print('ADAPTERS:' + ','.join(a.name for a in iter_payload_adapters())); "
    "print('SCAFFOLDS:' + ','.join(sorted(build_scaffold_registry())))"
)


@pytest.mark.component
def test_fake_payload_registered_via_entry_point(tmp_path: Path) -> None:
    """Install the fake dist into a fresh venv and assert the adapter and
    its scaffold verb are discovered without any framework edit."""
    if shutil.which("uv") is None:
        pytest.skip("uv is required for the entry-point extensibility test")

    venv_dir = tmp_path / "venv"
    dist_dir = _materialise_fake_dist(tmp_path)

    subprocess.run(
        ["uv", "venv", str(venv_dir), "--python", sys.executable],
        check=True,
    )
    venv_python = venv_dir / "bin" / "python"

    install = subprocess.run(
        uv_pip_install_cmd(str(venv_python), str(PROJECT_ROOT), str(dist_dir)),
        capture_output=True,
        text=True,
    )
    require_uv_install(install, "uv pip install (fake payload entry point)")

    probe = subprocess.run(
        [str(venv_python), "-c", _PROBE],
        capture_output=True,
        text=True,
        check=True,
    )
    out = probe.stdout
    adapters = next(line[len("ADAPTERS:") :] for line in out.splitlines() if line.startswith("ADAPTERS:"))
    scaffolds = next(line[len("SCAFFOLDS:") :] for line in out.splitlines() if line.startswith("SCAFFOLDS:"))

    assert "fake_payload" in adapters.split(","), (
        f"FakePayloadAdapter was not discovered via the "
        f"agent_foundry.payload_adapters entry point. Adapters: {adapters!r}"
    )
    assert "agent" in adapters.split(","), (
        f"Built-in agent adapter should remain registered alongside third-party ones. Got: {adapters!r}"
    )
    assert "fake_payload" in scaffolds.split(","), (
        f"The third-party payload's scaffold verb was not composed into the registry. Scaffolds: {scaffolds!r}"
    )
