"""Tests for helper functions in bricks/agent/agent.py.

Requires the ``openai-agents`` and ``databricks-sdk`` packages.
Skipped automatically if they are not installed.
"""

from __future__ import annotations

import os
import warnings
from pathlib import Path
from unittest import mock

import pytest

pytest.importorskip("agents", reason="openai-agents SDK not installed")
pytest.importorskip("databricks.sdk", reason="databricks-sdk not installed")

from payloads.agent.runtime.instance_paths import resolve_instance_dir as _resolve_instance_dir  # noqa: E402


class TestResolveInstanceDir:
    # Issue #143: ``project_root()`` is now strict (raises when
    # AGENT_FOUNDRY_PROJECT_ROOT is unset). These tests all pass
    # absolute ``INSTANCE_DIR`` values so the project-root resolver is
    # never invoked, but we set the env var defensively to keep the
    # tests hermetic against future changes to the resolution chain.
    def test_env_var_absolute(self, tmp_path: Path) -> None:
        inst = tmp_path / "my-app"
        inst.mkdir()
        (inst / "foundry.toml").write_text('name = "my-app"\n')

        with mock.patch.dict(
            os.environ,
            {
                "INSTANCE_DIR": str(inst),
                "AGENT_FOUNDRY_PROJECT_ROOT": str(tmp_path),
            },
        ):
            result = _resolve_instance_dir()
        assert result == inst

    def test_env_var_missing_marker(self, tmp_path: Path) -> None:
        inst = tmp_path / "no-marker"
        inst.mkdir()

        with mock.patch.dict(
            os.environ,
            {
                "INSTANCE_DIR": str(inst),
                "AGENT_FOUNDRY_PROJECT_ROOT": str(tmp_path),
            },
        ):
            result = _resolve_instance_dir()
        assert result is None

    def test_no_env_var_no_marker(self, tmp_path: Path) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "AGENT_FOUNDRY_PROJECT_ROOT": str(tmp_path),
            },
            clear=False,
        ):
            os.environ.pop("INSTANCE_DIR", None)
            result = _resolve_instance_dir()
        # The walk-up fallback may land on the agent_foundry repo itself
        # (which carries a foundry.toml of its own); when it does, that
        # is a valid instance dir.
        assert result is None or _has_instance_marker(result)

    def test_env_var_accepts_foundry_toml(self, tmp_path: Path) -> None:
        """Issue #295: foundry.toml is a first-class instance marker."""
        inst = tmp_path / "fancy-agent"
        inst.mkdir()
        (inst / "foundry.toml").write_text('name = "fancy-agent"\n')

        with (
            mock.patch.dict(
                os.environ,
                {
                    "INSTANCE_DIR": str(inst),
                    "AGENT_FOUNDRY_PROJECT_ROOT": str(tmp_path),
                },
            ),
            warnings.catch_warnings(record=True) as caught,
        ):
            warnings.simplefilter("always")
            result = _resolve_instance_dir()

        assert result == inst
        # No DeprecationWarning for the preferred marker.
        assert not any(issubclass(w.category, DeprecationWarning) for w in caught), [
            (w.category, str(w.message)) for w in caught
        ]

    def test_env_var_ignores_legacy_instance_yml(self, tmp_path: Path) -> None:
        inst = tmp_path / "legacy-app"
        inst.mkdir()
        (inst / "instance.yml").write_text("name: legacy-app\n")

        with mock.patch.dict(
            os.environ,
            {"INSTANCE_DIR": str(inst), "AGENT_FOUNDRY_PROJECT_ROOT": str(tmp_path)},
        ):
            assert _resolve_instance_dir() is None


def _has_instance_marker(directory: Path) -> bool:
    return (directory / "foundry.toml").exists()
