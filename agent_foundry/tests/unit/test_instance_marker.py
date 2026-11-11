"""Tests for ``agent_foundry._instance_marker`` (issue #295)."""

from __future__ import annotations

from pathlib import Path

from agent_foundry._instance_marker import INSTANCE_MARKER, is_instance_dir


def test_marker_is_foundry_toml() -> None:
    assert INSTANCE_MARKER == "foundry.toml"


def test_is_instance_dir_foundry_toml(tmp_path: Path) -> None:
    (tmp_path / "foundry.toml").write_text('name = "x"\n')
    assert is_instance_dir(tmp_path) is True


def test_is_instance_dir_ignores_legacy_marker(tmp_path: Path) -> None:
    (tmp_path / "instance.yml").write_text("name: x\n")
    assert is_instance_dir(tmp_path) is False


def test_is_instance_dir_empty(tmp_path: Path) -> None:
    assert is_instance_dir(tmp_path) is False
