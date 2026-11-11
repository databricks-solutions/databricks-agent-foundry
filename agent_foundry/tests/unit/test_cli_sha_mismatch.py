"""Deploy aborts when the running framework SHA disagrees with the pin."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_foundry.deploy.cli.deploy import _fail_on_framework_sha_mismatch


def test_fail_on_framework_sha_mismatch_ok(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "agent_foundry._meta.compare_pinned_sha",
        lambda _root: SimpleNamespace(matches=True, message="ok"),
    )
    assert _fail_on_framework_sha_mismatch(tmp_path) == 0


def test_fail_on_framework_sha_mismatch_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "agent_foundry._meta.compare_pinned_sha",
        lambda _root: SimpleNamespace(
            matches=False,
            message="pinned abc1234 != running deadbeef",
        ),
    )
    assert _fail_on_framework_sha_mismatch(tmp_path) == 1
    out = capsys.readouterr().out
    assert "ERROR: agent-foundry version mismatch" in out
    assert "pinned abc1234 != running deadbeef" in out
