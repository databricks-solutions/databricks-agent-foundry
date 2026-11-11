"""Tests for ``agent_foundry.validate`` (issue #96).

Narrow coverage of the ``--offline`` path -- no live workspace.  The
workspace branch is exercised in integration tests; here we only assert
the public ``run_checks(...)`` API and the moved CLI signature behave
like the legacy ``scripts/validate_prerequisites.py`` did.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_foundry import validate as validate_mod


def _seed_minimal_project(root: Path) -> None:
    """Lay down the bare files ``check_project_structure`` looks for."""
    (root / "pyproject.toml").write_text("[project]\nname='dummy'\n")


def test_run_checks_offline_passes_on_minimal_project(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed_minimal_project(tmp_path)
    rc = validate_mod.run_checks(offline=True, workspace=False, project_root=tmp_path)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Validate" in out
    assert "Prerequisites:" in out
    assert "Project:" in out
    assert "Framework:" in out


def test_run_checks_offline_fails_when_required_files_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = validate_mod.run_checks(offline=True, workspace=False, project_root=tmp_path)
    assert rc == 1
    out = capsys.readouterr().out
    assert "FAIL" in out
    assert "MISSING" in out


def test_check_instances_reports_zero_when_directory_absent(tmp_path: Path) -> None:
    result = validate_mod.check_instances(tmp_path)
    assert result.passed
    assert "none configured" in result.detail


def test_check_instances_lists_discovered_foundry_toml(tmp_path: Path) -> None:
    inst = tmp_path / "instances" / "demo"
    inst.mkdir(parents=True)
    (inst / "foundry.toml").write_text('name = "demo"\n')

    result = validate_mod.check_instances(tmp_path)
    assert result.passed
    assert "demo" in result.detail


def test_discover_instance_dirs_ignores_legacy_marker(tmp_path: Path) -> None:
    inst = tmp_path / "instances" / "legacy-app"
    inst.mkdir(parents=True)
    (inst / "instance.yml").write_text("name: legacy-app\n")

    assert validate_mod.discover_instance_dirs(tmp_path) == []


def test_check_legacy_layout_fails_on_leftover_files(tmp_path: Path) -> None:
    targets = tmp_path / "conf" / "targets"
    targets.mkdir(parents=True)
    (targets / "dev.yml").write_text("variables: {}\n")

    result = validate_mod.check_legacy_layout(tmp_path)
    assert not result.passed
    assert "dev.yml" in result.detail


def test_check_legacy_layout_passes_on_clean_repo(tmp_path: Path) -> None:
    assert validate_mod.check_legacy_layout(tmp_path).passed


def test_run_checks_fails_on_leftover_legacy_files(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed_minimal_project(tmp_path)
    inst = tmp_path / "instances" / "demo"
    inst.mkdir(parents=True)
    (inst / "instance.yml").write_text("name: demo\n")

    rc = validate_mod.run_checks(offline=True, workspace=False, project_root=tmp_path)

    assert rc == 1
    assert "Config layout" in capsys.readouterr().out


def test_discover_instance_dirs_foundry_toml(tmp_path: Path) -> None:
    (tmp_path / "foundry.toml").write_text(
        '[[instances]]\npath = "instances/test-agent"\n',
    )
    inst = tmp_path / "instances" / "test-agent"
    inst.mkdir(parents=True)
    (inst / "foundry.toml").write_text("[instance]\nname = 'test-agent'\n")

    dirs = validate_mod.discover_instance_dirs(tmp_path)
    assert [d.name for d in dirs] == ["test-agent"]


def test_summarize_brick_registry() -> None:
    results = validate_mod.check_brick_registry()
    summary = validate_mod.summarize_brick_registry(results)
    assert summary.passed
    assert "registered" in summary.detail


def test_check_brick_registry_lists_all_builtin_bricks() -> None:
    results = validate_mod.check_brick_registry()
    assert results
    assert all(r.passed for r in results)
    names = {r.name for r in results}
    assert "Brick: SQL Warehouse" in names
    assert "Brick: Genie Spaces" in names


def test_check_project_structure_only_checks_pyproject(tmp_path: Path) -> None:
    results = validate_mod.check_project_structure(tmp_path)
    assert len(results) == 1
    assert results[0].name == "File: pyproject.toml"
    assert not results[0].passed


def test_load_target_variables_reads_foundry_toml_profile(tmp_path: Path) -> None:
    """foundry.toml repos must surface the target profile/host.

    Regression for the ``validate --workspace`` 401: the legacy loader
    ignored foundry.toml, so the workspace client fell back to ambiguous
    ambient auth (multiple ~/.databrickscfg profiles sharing a host).
    """
    (tmp_path / "foundry.toml").write_text(
        "schema_version = 2\n\n"
        "[targets.dev]\n"
        'host = "https://ws.cloud.databricks.com"\n'
        'catalog = "cat"\n'
        'profile = "my-profile"\n\n'
        '[[instances]]\npath = "instances/demo"\n'
    )
    variables = validate_mod._load_target_variables(tmp_path, "dev")
    assert variables["profile"] == "my-profile"
    assert variables["host"] == "https://ws.cloud.databricks.com"


def test_load_target_variables_unknown_target_returns_empty(tmp_path: Path) -> None:
    (tmp_path / "foundry.toml").write_text('schema_version = 2\n\n[targets.dev]\nprofile = "p"\n')
    assert validate_mod._load_target_variables(tmp_path, "staging") == {}


def test_load_target_variables_without_foundry_toml_returns_empty(tmp_path: Path) -> None:
    assert validate_mod._load_target_variables(tmp_path, "dev") == {}


def test_main_offline_returns_int(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_minimal_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    rc = validate_mod.main([])
    assert rc == 0
