"""Tests for the bundled-app process manager's chat-UI build sequence.

Focus: the Lakebase ``db:migrate`` step must be non-fatal so a transient
post-SP-rotation "permission denied for schema" error cannot crash the
whole app, while the real ``build:client`` / ``build:server`` bundles
stay fatal.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from bricks.app.scripts.start_app import _CHAT_UI_BUILD_STEPS, ProcessManager


def _result(returncode: int) -> mock.MagicMock:
    return mock.MagicMock(returncode=returncode, stdout="", stderr="")


class TestChatUiBuildSteps:
    def test_db_migrate_is_the_only_non_fatal_step(self) -> None:
        non_fatal = [cmd for cmd, fatal in _CHAT_UI_BUILD_STEPS if not fatal]
        assert non_fatal == ["npm run db:migrate"]

    def test_real_build_steps_present_and_fatal(self) -> None:
        fatal = {cmd for cmd, fatal in _CHAT_UI_BUILD_STEPS if fatal}
        assert {"npm install", "npm run build:client", "npm run build:server"} <= fatal


class TestBuildChatUi:
    @mock.patch("bricks.app.scripts.start_app.subprocess.run")
    def test_db_migrate_failure_does_not_abort(self, mock_run: mock.MagicMock) -> None:
        mock_run.side_effect = lambda cmd, **_: _result(1 if cmd == ["npm", "run", "db:migrate"] else 0)
        assert ProcessManager()._build_chat_ui(Path("/tmp/ui")) is True

    @mock.patch("bricks.app.scripts.start_app.subprocess.run")
    def test_build_client_failure_aborts(self, mock_run: mock.MagicMock) -> None:
        mock_run.side_effect = lambda cmd, **_: _result(1 if cmd == ["npm", "run", "build:client"] else 0)
        assert ProcessManager()._build_chat_ui(Path("/tmp/ui")) is False

    @mock.patch("bricks.app.scripts.start_app.subprocess.run")
    def test_install_failure_aborts(self, mock_run: mock.MagicMock) -> None:
        mock_run.side_effect = lambda cmd, **_: _result(1 if cmd == ["npm", "install"] else 0)
        assert ProcessManager()._build_chat_ui(Path("/tmp/ui")) is False

    @mock.patch("bricks.app.scripts.start_app.subprocess.run")
    def test_all_steps_succeed(self, mock_run: mock.MagicMock) -> None:
        mock_run.return_value = _result(0)
        assert ProcessManager()._build_chat_ui(Path("/tmp/ui")) is True


class TestApplyOverrides:
    def test_extra_overlay_wins_over_agent_overrides(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        agent_dir = tmp_path / "agent"
        extension_pkg = tmp_path / "pkgs" / "fake_overlay_pkg"
        extension_dir = extension_pkg / "ui" / "overrides"
        chat_ui = tmp_path / "project" / "e2e-chatbot-app-next"
        for directory, content in ((agent_dir, "agent"), (extension_dir, "extension")):
            (directory / "client").mkdir(parents=True)
            (directory / "client" / "registry.tsx").write_text(content)
            (directory / "MANIFEST.md").write_text("docs")
        (agent_dir / "client" / "only-agent.tsx").write_text("kept")
        (extension_pkg / "__init__.py").write_text("")
        monkeypatch.syspath_prepend(str(tmp_path / "pkgs"))
        monkeypatch.setenv("CHAT_UI_OVERLAYS", "fake_overlay_pkg")

        with (
            mock.patch("bricks.app.scripts.start_app.OVERRIDES_DIR", agent_dir),
            mock.patch("bricks.app.scripts.start_app.PROJECT_ROOT", tmp_path / "project"),
        ):
            ProcessManager._apply_overrides()

        assert (chat_ui / "client" / "registry.tsx").read_text() == "extension"
        assert (chat_ui / "client" / "only-agent.tsx").read_text() == "kept"
        assert not (chat_ui / "MANIFEST.md").exists()
