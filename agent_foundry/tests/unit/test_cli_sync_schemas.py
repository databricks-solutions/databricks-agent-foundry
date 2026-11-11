"""Tests for ``agent-foundry sync-schemas`` (issue #191, F-PR3)."""

from __future__ import annotations

import json
from pathlib import Path

import click
from click.testing import Result
from typer.testing import CliRunner

from agent_foundry.deploy.cli import app
from agent_foundry.schemas import SCHEMA_BUNDLE

# Match the existing test_cli.py click-version handling so the runner
# works on both click 8.1 and 8.2+.
_CLICK_MAJOR, _CLICK_MINOR, *_ = (int(p) for p in click.__version__.split(".")[:2])
_CLI_RUNNER_KWARGS: dict = {} if (_CLICK_MAJOR, _CLICK_MINOR) >= (8, 2) else {"mix_stderr": False}


def _stderr(result: Result) -> str:
    try:
        return result.stderr or ""
    except ValueError:
        return ""


# ---------------------------------------------------------------------------
# Help + smoke
# ---------------------------------------------------------------------------


def test_sync_schemas_help_smoke() -> None:
    runner = CliRunner(**_CLI_RUNNER_KWARGS)
    result = runner.invoke(app, ["sync-schemas", "--help"])
    assert result.exit_code == 0, result.output
    assert "sync-schemas" in result.output or "Copy the JSON Schemas" in result.output


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_sync_schemas_writes_full_bundle_to_default_target(tmp_path: Path) -> None:
    """Default ``--target-dir`` is ``.agent-foundry/schemas`` relative
    to ``--repo-root`` -- exactly the path the F-PR3 contract pins.
    """
    runner = CliRunner(**_CLI_RUNNER_KWARGS)
    result = runner.invoke(
        app,
        ["sync-schemas", "--repo-root", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output + _stderr(result)
    written = sorted(p.name for p in (tmp_path / ".agent-foundry" / "schemas").iterdir())
    assert written == sorted(SCHEMA_BUNDLE.keys())


def test_sync_schemas_respects_explicit_target_dir(tmp_path: Path) -> None:
    runner = CliRunner(**_CLI_RUNNER_KWARGS)
    custom_dir = tmp_path / "custom-schemas"
    result = runner.invoke(
        app,
        [
            "sync-schemas",
            "--repo-root",
            str(tmp_path),
            "--target-dir",
            str(custom_dir),
        ],
    )
    assert result.exit_code == 0, result.output + _stderr(result)
    assert (custom_dir / "foundry-repo.schema.json").exists()
    assert (custom_dir / "foundry-instance.schema.json").exists()


def test_sync_schemas_writes_valid_json(tmp_path: Path) -> None:
    """The copied files must parse as JSON and match the wheel bytes."""
    runner = CliRunner(**_CLI_RUNNER_KWARGS)
    result = runner.invoke(
        app,
        ["sync-schemas", "--repo-root", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output + _stderr(result)
    body = (tmp_path / ".agent-foundry" / "schemas" / "foundry-repo.schema.json").read_text()
    parsed = json.loads(body)
    assert parsed["title"] == "FoundryRepoConfig"
    assert parsed["additionalProperties"] is False


# ---------------------------------------------------------------------------
# Overwrite protection
# ---------------------------------------------------------------------------


def test_sync_schemas_refuses_to_clobber_unknown_files(tmp_path: Path) -> None:
    """If the target dir already exists and contains a file we don't
    own, ``sync-schemas`` refuses to write without ``--force``.  Protects
    a downstream operator who accidentally points the verb at the
    wrong directory.
    """
    runner = CliRunner(**_CLI_RUNNER_KWARGS)
    target = tmp_path / ".agent-foundry" / "schemas"
    target.mkdir(parents=True)
    (target / "user_owned.txt").write_text("hi")

    result = runner.invoke(
        app,
        ["sync-schemas", "--repo-root", str(tmp_path)],
    )
    assert result.exit_code == 2, result.output + _stderr(result)
    assert "user_owned.txt" in (result.output + _stderr(result))
    # User file must not have been touched.
    assert (target / "user_owned.txt").read_text() == "hi"


def test_sync_schemas_force_overwrites(tmp_path: Path) -> None:
    """``--force`` writes through even when unknown files are present."""
    runner = CliRunner(**_CLI_RUNNER_KWARGS)
    target = tmp_path / ".agent-foundry" / "schemas"
    target.mkdir(parents=True)
    (target / "user_owned.txt").write_text("hi")
    # Pre-existing schema-shaped file with stale bytes.
    (target / "foundry-repo.schema.json").write_text("{}")

    result = runner.invoke(
        app,
        ["sync-schemas", "--repo-root", str(tmp_path), "--force"],
    )
    assert result.exit_code == 0, result.output + _stderr(result)
    # Stale schema is now real.
    refreshed = json.loads((target / "foundry-repo.schema.json").read_text())
    assert refreshed["title"] == "FoundryRepoConfig"


def test_sync_schemas_overwrites_existing_schema_files_silently(tmp_path: Path) -> None:
    """Existing files in the bundle (managed by sync-schemas) are
    overwritten freely without --force.  Only foreign files trigger
    the refusal.
    """
    runner = CliRunner(**_CLI_RUNNER_KWARGS)
    target = tmp_path / ".agent-foundry" / "schemas"
    target.mkdir(parents=True)
    (target / "foundry-repo.schema.json").write_text("{}")  # stale

    result = runner.invoke(
        app,
        ["sync-schemas", "--repo-root", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output + _stderr(result)
    refreshed = json.loads((target / "foundry-repo.schema.json").read_text())
    assert refreshed["title"] == "FoundryRepoConfig"
