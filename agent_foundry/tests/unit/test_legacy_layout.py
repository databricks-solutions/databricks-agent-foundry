"""Tests for ``agent_foundry._legacy_layout.reject_legacy_layout``."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_foundry._legacy_layout import LegacyLayoutError, find_legacy_files, reject_legacy_layout
from agent_foundry.loader import FoundryConfigError


def _seed_foundry_repo(root: Path) -> Path:
    (root / "foundry.toml").write_text("schema_version = 2\n")
    instances = root / "instances"
    (instances / "app").mkdir(parents=True)
    (instances / "app" / "foundry.toml").write_text('name = "app"\n')
    return instances


def test_clean_foundry_repo_passes(tmp_path: Path) -> None:
    instances = _seed_foundry_repo(tmp_path)

    reject_legacy_layout(tmp_path, instances)


def test_missing_dirs_pass(tmp_path: Path) -> None:
    reject_legacy_layout(tmp_path, tmp_path / "instances")


@pytest.mark.parametrize(
    "legacy_file",
    ["conf/targets/dev.yml", "instances/app/instance.yml"],
)
def test_any_legacy_file_is_rejected(tmp_path: Path, legacy_file: str) -> None:
    instances = _seed_foundry_repo(tmp_path)
    path = tmp_path / legacy_file
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("variables: {}\n")

    with pytest.raises(LegacyLayoutError) as excinfo:
        reject_legacy_layout(tmp_path, instances)

    message = str(excinfo.value)
    assert str(path) in message
    assert "no longer supported" in message
    assert "docs/reference/config.md" in message


def test_lists_every_legacy_file(tmp_path: Path) -> None:
    instances = _seed_foundry_repo(tmp_path)
    targets = tmp_path / "conf" / "targets"
    targets.mkdir(parents=True)
    (targets / "dev.yml").write_text("")
    (targets / "prod.yml").write_text("")
    (instances / "app" / "instance.yml").write_text("")

    found = find_legacy_files(tmp_path, instances)

    assert found == [targets / "dev.yml", targets / "prod.yml", instances / "app" / "instance.yml"]


def test_non_target_conf_files_are_ignored(tmp_path: Path) -> None:
    conf = tmp_path / "conf"
    conf.mkdir()
    (conf / "mcp_servers.yml").write_text("")

    assert find_legacy_files(tmp_path, tmp_path / "instances") == []


def test_error_is_a_foundry_config_error() -> None:
    assert issubclass(LegacyLayoutError, FoundryConfigError)
