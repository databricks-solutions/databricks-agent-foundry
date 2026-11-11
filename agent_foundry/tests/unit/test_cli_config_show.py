"""CLI smoke tests for ``agent-foundry config show`` (#191, F-PR4).

The library layer is covered exhaustively in
``agent_foundry/tests/unit/test_foundry_inspect.py``.  These tests assert only what's
specific to the Typer wrapper: exit codes, the ``--format``
validation guard, error surfacing, and that the sub-app is wired
into the top-level ``app`` correctly.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import click
from click.testing import Result
from typer.testing import CliRunner

from agent_foundry.deploy.cli import app
from agent_foundry.pytest_plugin import strip_ansi

_CLICK_MAJOR, _CLICK_MINOR, *_ = (int(p) for p in click.__version__.split(".")[:2])
_CLI_RUNNER_KWARGS: dict = {} if (_CLICK_MAJOR, _CLICK_MINOR) >= (8, 2) else {"mix_stderr": False}


def _stderr(result: Result) -> str:
    try:
        return result.stderr or ""
    except ValueError:
        return ""


def _scaffold(tmp_path: Path) -> Path:
    """Same minimal layout the inspect tests use, copied here to keep
    each test file self-contained.
    """
    (tmp_path / "foundry.toml").write_text(
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
    inst = tmp_path / "instances" / "demo"
    inst.mkdir(parents=True)
    (inst / "foundry.toml").write_text(
        textwrap.dedent(
            """
            name = "demo"
            description = "test instance"
            schema_prefix = "demo_app"
            vs_endpoint_name = "vs-demo"
            app_name = "demo-app"
            """
        ).lstrip(),
        encoding="utf-8",
    )
    return tmp_path


# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------


def test_config_group_help() -> None:
    runner = CliRunner(**_CLI_RUNNER_KWARGS)
    result = runner.invoke(app, ["config", "--help"])
    assert result.exit_code == 0, result.output
    assert "show" in result.output


def test_config_show_help() -> None:
    runner = CliRunner(**_CLI_RUNNER_KWARGS)
    result = runner.invoke(app, ["config", "show", "--help"])
    assert result.exit_code == 0, result.output
    output = strip_ansi(result.output)
    assert "--resolved" in output
    assert "--include-data" in output


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_config_show_default_json_format(tmp_path: Path) -> None:
    _scaffold(tmp_path)
    runner = CliRunner(**_CLI_RUNNER_KWARGS)
    result = runner.invoke(
        app,
        ["config", "show", "--target", "dev", "--repo-root", str(tmp_path), "-f", "json"],
    )
    assert result.exit_code == 0, result.output + _stderr(result)
    parsed = json.loads(result.output)
    assert parsed["target"]["name"] == "dev"
    assert parsed["instance"]["name"] == "demo"


def test_config_show_resolved_view(tmp_path: Path) -> None:
    """``--resolved`` swaps to the runtime InstanceConfig view; the
    shape differs from the authored view (no top-level ``target`` /
    ``instance`` split).
    """
    _scaffold(tmp_path)
    runner = CliRunner(**_CLI_RUNNER_KWARGS)
    result = runner.invoke(
        app,
        [
            "config",
            "show",
            "--target",
            "dev",
            "--repo-root",
            str(tmp_path),
            "--resolved",
            "-f",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output + _stderr(result)
    parsed = json.loads(result.output)
    assert parsed["name"] == "demo"
    assert parsed["target"]["catalog"] == "main"


def test_config_show_toml_is_default(tmp_path: Path) -> None:
    _scaffold(tmp_path)
    runner = CliRunner(**_CLI_RUNNER_KWARGS)
    result = runner.invoke(
        app,
        ["config", "show", "--target", "dev", "--repo-root", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output + _stderr(result)
    # TOML output uses [section] headers and key = value syntax.
    assert "[target]" in result.output
    assert 'name = "demo"' in result.output


# ---------------------------------------------------------------------------
# Error surfacing
# ---------------------------------------------------------------------------


def test_config_show_rejects_unknown_format(tmp_path: Path) -> None:
    _scaffold(tmp_path)
    runner = CliRunner(**_CLI_RUNNER_KWARGS)
    result = runner.invoke(
        app,
        ["config", "show", "--target", "dev", "--repo-root", str(tmp_path), "-f", "xml"],
    )
    assert result.exit_code == 2, result.output + _stderr(result)
    assert "Unknown --format" in (result.output + _stderr(result))


def test_config_show_surfaces_unknown_target(tmp_path: Path) -> None:
    """Unknown target -> exit 1, message lists what's available."""
    _scaffold(tmp_path)
    runner = CliRunner(**_CLI_RUNNER_KWARGS)
    result = runner.invoke(
        app,
        ["config", "show", "--target", "prod", "--repo-root", str(tmp_path)],
    )
    assert result.exit_code == 1, result.output + _stderr(result)
    combined = result.output + _stderr(result)
    assert "prod" in combined
    assert "Available targets" in combined
    assert "dev" in combined


def test_config_show_requires_target_flag(tmp_path: Path) -> None:
    """``--target`` is marked required (``...``); Typer should reject
    invocations that omit it.
    """
    _scaffold(tmp_path)
    runner = CliRunner(**_CLI_RUNNER_KWARGS)
    result = runner.invoke(
        app,
        ["config", "show", "--repo-root", str(tmp_path)],
    )
    assert result.exit_code != 0
    assert "target" in (result.output + _stderr(result)).lower()
