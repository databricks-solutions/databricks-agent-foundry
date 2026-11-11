"""Tests for :mod:`payloads.agent.runtime.instance_paths`."""

from __future__ import annotations

from pathlib import Path

import pytest

from payloads.agent.runtime.instance_paths import project_root

# ---------------------------------------------------------------------------
# Issue #143: explicit runtime project-root resolution
# ---------------------------------------------------------------------------


class TestProjectRoot:
    """Verify the env-var-only contract for ``project_root()``."""

    def test_returns_resolved_path_when_env_set(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENT_FOUNDRY_PROJECT_ROOT", str(tmp_path))
        assert project_root() == tmp_path.resolve()

    def test_raises_when_env_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AGENT_FOUNDRY_PROJECT_ROOT", raising=False)
        with pytest.raises(RuntimeError, match="AGENT_FOUNDRY_PROJECT_ROOT"):
            project_root()

    def test_raises_when_env_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENT_FOUNDRY_PROJECT_ROOT", "")
        with pytest.raises(RuntimeError, match="AGENT_FOUNDRY_PROJECT_ROOT"):
            project_root()

    def test_raises_when_path_is_not_a_directory(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        not_a_dir = tmp_path / "does-not-exist"
        monkeypatch.setenv("AGENT_FOUNDRY_PROJECT_ROOT", str(not_a_dir))
        with pytest.raises(RuntimeError, match="not a directory"):
            project_root()
