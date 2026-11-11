"""End-to-end tests for ``agent-foundry deploy`` on ``foundry.toml`` (#191).

Given a synthetic two-file repo, does the verb hand the right
variables to ``deploy_instance`` and surface errors correctly?
Leftover legacy files are unit-tested in ``test_legacy_layout.py``;
here we only check the verbs refuse to run.

``deploy_instance`` is monkeypatched everywhere so these tests
don't need a Databricks workspace.  The mock captures its
arguments so we can assert on the variable bag the new path
produces.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import click
import pytest
from click.testing import Result
from typer.testing import CliRunner

from agent_foundry.deploy.cli import app
from agent_foundry.public import DeployResult

_CLICK_MAJOR, _CLICK_MINOR, *_ = (int(p) for p in click.__version__.split(".")[:2])
_CLI_RUNNER_KWARGS: dict = {} if (_CLICK_MAJOR, _CLICK_MINOR) >= (8, 2) else {"mix_stderr": False}


def _combined(result: Result) -> str:
    stdout = result.output or ""
    try:
        stderr = result.stderr or ""
    except ValueError:
        stderr = ""
    return stdout + stderr


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _scaffold_foundry_toml_repo(
    repo: Path,
    *,
    extra_instance_toml: str = "",
) -> Path:
    """Materialise the minimal two-file foundry.toml layout."""
    (repo / "foundry.toml").write_text(
        textwrap.dedent(
            """
            schema_version = 2

            [targets.dev]
            host = "https://dev.cloud.databricks.com"
            catalog = "main"
            profile = "dev-profile"

            [[instances]]
            path = "instances/demo"
            """
        ).lstrip(),
        encoding="utf-8",
    )
    inst = repo / "instances" / "demo"
    inst.mkdir(parents=True)
    (inst / "foundry.toml").write_text(
        textwrap.dedent(
            f"""
            name = "demo"
            description = "test instance"
            schema_prefix = "demo_app"
            vs_endpoint_name = "vs-demo"
            app_name = "demo-app"
            {extra_instance_toml}
            """
        ).lstrip(),
        encoding="utf-8",
    )
    return inst


def _patch_deploy_instance(
    monkeypatch: pytest.MonkeyPatch,
    *,
    captured: list[dict[str, Any]] | None = None,
    error: bool = False,
):
    """Stub out ``deploy_instance`` to skip the real brick loop.

    When *captured* is supplied, each call's kwargs are appended so
    tests can assert on the variable bag the new path produced.
    """

    def _fake_deploy_instance(
        instance_dir: Path,
        target: str,
        *,
        variables: dict[str, str] | None = None,
        dry_run: bool = False,
        skip_bricks: set[str] | None = None,
        only_bricks: set[str] | None = None,
        instance_config: object | None = None,
        ws: object | None = None,
        offline: bool = False,
    ) -> DeployResult:
        if captured is not None:
            captured.append(
                {
                    "instance_dir": instance_dir,
                    "target": target,
                    "instance_config": instance_config,
                    "variables": dict(variables or {}),
                    "dry_run": dry_run,
                    "skip_bricks": set(skip_bricks or set()),
                    "only_bricks": set(only_bricks or set()),
                }
            )
        bricks: dict[str, list[Any]] = {}
        if error:
            from agent_foundry.bricks import BrickResult

            bricks["fake"] = [BrickResult(name="fake", status="error: synthetic")]
        return DeployResult(
            instance_name=instance_dir.name,
            target=target,
            bricks=bricks,
        )

    monkeypatch.setattr("agent_foundry.deploy.cli.deploy.deploy_instance", _fake_deploy_instance)


def _patch_deployer_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the SDK-backed deployer lookup so dry-run-off tests work
    offline.  Mirrors the same trick the legacy CLI tests use.
    """

    def _fake_resolve(variables: dict[str, str], dry_run: bool = False, **_k: object) -> dict[str, str]:
        # Mirror the real ``resolve_deployer``: an empty string counts
        # as "unresolved" (the runtime ``TargetConfig.deployer`` field
        # defaults to "" rather than None).
        if not variables.get("deployer"):
            variables = dict(variables)
            variables["deployer"] = "test-user@example.com"
        return variables

    monkeypatch.setattr("agent_foundry.deploy.cli.deploy.resolve_deployer", _fake_resolve)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_deploy_foundry_toml_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A foundry.toml repo deploys every declared instance."""
    _scaffold_foundry_toml_repo(tmp_path)
    captured: list[dict[str, Any]] = []
    _patch_deploy_instance(monkeypatch, captured=captured)
    _patch_deployer_resolution(monkeypatch)
    monkeypatch.chdir(tmp_path)

    runner = CliRunner(**_CLI_RUNNER_KWARGS)
    result = runner.invoke(app, ["deploy", "--target", "dev", "--dry-run"])
    assert result.exit_code == 0, _combined(result)
    # One instance declared, one deploy_instance call expected.
    assert len(captured) == 1


def test_deploy_foundry_toml_passes_resolved_variables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The new path's variable bag must come from
    ``InstanceConfig.to_variables()``.  Spot-check a handful of keys
    that bricks rely on (catalog, schema_prefix, app_name, deployer).
    """
    _scaffold_foundry_toml_repo(tmp_path)
    captured: list[dict[str, Any]] = []
    _patch_deploy_instance(monkeypatch, captured=captured)
    _patch_deployer_resolution(monkeypatch)
    monkeypatch.chdir(tmp_path)

    runner = CliRunner(**_CLI_RUNNER_KWARGS)
    result = runner.invoke(app, ["deploy", "--target", "dev", "--dry-run"])
    assert result.exit_code == 0, _combined(result)
    bag = captured[0]["variables"]
    assert bag["catalog"] == "main"
    assert bag["schema_prefix"] == "demo_app"
    assert bag["app_name"] == "demo-app"
    assert bag["deployer"] == "test-user@example.com"
    # The new path resolves deployer once and folds it back into the
    # variable bag so bricks see it via ${var.deployer}.
    assert "deployer" in bag


def test_deploy_foundry_toml_threads_cli_var_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _scaffold_foundry_toml_repo(tmp_path)
    captured: list[dict[str, Any]] = []
    _patch_deploy_instance(monkeypatch, captured=captured)
    _patch_deployer_resolution(monkeypatch)
    monkeypatch.chdir(tmp_path)

    runner = CliRunner(**_CLI_RUNNER_KWARGS)
    result = runner.invoke(
        app,
        ["deploy", "--target", "dev", "--dry-run", "--vars", "extra_key=hello"],
    )
    assert result.exit_code == 0, _combined(result)
    assert captured[0]["variables"]["extra_key"] == "hello"


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "legacy_file",
    ["instances/demo/instance.yml", "conf/targets/dev.yml"],
)
def test_deploy_rejects_leftover_legacy_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, legacy_file: str
) -> None:
    """Leftover legacy files = exit 1, no deploy_instance call, and a
    message naming the file.
    """
    _scaffold_foundry_toml_repo(tmp_path)
    path = tmp_path / legacy_file
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("variables: {}\n", encoding="utf-8")
    captured: list[dict[str, Any]] = []
    _patch_deploy_instance(monkeypatch, captured=captured)
    monkeypatch.chdir(tmp_path)

    runner = CliRunner(**_CLI_RUNNER_KWARGS)
    result = runner.invoke(app, ["deploy", "--target", "dev", "--dry-run"])
    assert result.exit_code == 1
    out = _combined(result)
    assert "no longer supported" in out
    assert path.name in out
    assert captured == []


def test_destroy_rejects_leftover_legacy_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _scaffold_foundry_toml_repo(tmp_path)
    (tmp_path / "instances" / "demo" / "instance.yml").write_text("name: demo\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    runner = CliRunner(**_CLI_RUNNER_KWARGS)
    result = runner.invoke(app, ["destroy", "--instance", "demo", "--target", "dev", "--dry-run", "--offline"])
    assert result.exit_code == 1
    assert "no longer supported" in _combined(result)


def test_deploy_errors_on_unknown_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _scaffold_foundry_toml_repo(tmp_path)
    captured: list[dict[str, Any]] = []
    _patch_deploy_instance(monkeypatch, captured=captured)
    _patch_deployer_resolution(monkeypatch)
    monkeypatch.chdir(tmp_path)

    runner = CliRunner(**_CLI_RUNNER_KWARGS)
    result = runner.invoke(app, ["deploy", "--target", "staging", "--dry-run"])
    assert result.exit_code == 1
    out = _combined(result)
    assert "staging" in out
    assert "Available targets" in out
    assert captured == []


def test_deploy_errors_on_empty_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No foundry.toml should fail loudly."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner(**_CLI_RUNNER_KWARGS)
    result = runner.invoke(app, ["deploy", "--target", "dev", "--dry-run"])
    assert result.exit_code == 1
    out = _combined(result)
    assert "No foundry.toml" in out


def test_deploy_errors_on_unknown_instance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--instance other`` when only ``demo`` is declared should
    surface the loader's typed error, not crash with a stack trace.
    """
    _scaffold_foundry_toml_repo(tmp_path)
    captured: list[dict[str, Any]] = []
    _patch_deploy_instance(monkeypatch, captured=captured)
    _patch_deployer_resolution(monkeypatch)
    monkeypatch.chdir(tmp_path)

    runner = CliRunner(**_CLI_RUNNER_KWARGS)
    result = runner.invoke(app, ["deploy", "--target", "dev", "--instance", "other", "--dry-run"])
    assert result.exit_code == 1
    assert captured == []
