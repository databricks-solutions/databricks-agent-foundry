"""Unit tests for ``_run_uv_sync`` / ``_run_uv_lock`` (init install)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import typer

from agent_foundry.deploy.cli.init import _run_uv_lock, _run_uv_sync


def test_run_uv_sync_returns_false_when_uv_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("agent_foundry.deploy.cli.init.shutil.which", lambda _name: None)

    assert _run_uv_sync(tmp_path) is False
    assert "uv not found" in capsys.readouterr().out


def test_run_uv_sync_passes_frozen_flag(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("agent_foundry.deploy.cli.init.shutil.which", lambda _name: "/usr/bin/uv")
    captured: list[list[str]] = []

    def _run(cmd: list[str], **_k: object) -> SimpleNamespace:
        captured.append(list(cmd))
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr("agent_foundry.deploy.cli.init.subprocess.run", _run)

    assert _run_uv_sync(tmp_path) is True
    assert captured == [["uv", "sync", "--frozen"]]


def test_run_uv_sync_returns_false_on_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("agent_foundry.deploy.cli.init.shutil.which", lambda _name: "/usr/bin/uv")

    def _fail(*_a, **_k):
        return subprocess.CompletedProcess(
            args=["uv", "sync", "--frozen"],
            returncode=1,
            stdout="",
            stderr="resolver exploded\n",
        )

    monkeypatch.setattr("agent_foundry.deploy.cli.init.subprocess.run", _fail)

    assert _run_uv_sync(tmp_path) is False
    err = capsys.readouterr().err
    assert "Could not install dependencies" in err
    assert "resolver exploded" in err


def test_run_uv_lock_returns_false_when_uv_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("agent_foundry.deploy.cli.init.shutil.which", lambda _name: None)

    assert _run_uv_lock(tmp_path) is False
    assert "skipping lockfile" in capsys.readouterr().out


def test_run_uv_lock_raises_exit_on_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("agent_foundry.deploy.cli.init.shutil.which", lambda _name: "/usr/bin/uv")

    def _fail(*_a, **_k):
        return subprocess.CompletedProcess(
            args=["uv", "lock"],
            returncode=1,
            stdout="",
            stderr="unsatisfiable: litellm==1.88.6\n",
        )

    monkeypatch.setattr("agent_foundry.deploy.cli.init.subprocess.run", _fail)

    with pytest.raises(typer.Exit) as exc_info:
        _run_uv_lock(tmp_path)
    assert exc_info.value.exit_code == 2
