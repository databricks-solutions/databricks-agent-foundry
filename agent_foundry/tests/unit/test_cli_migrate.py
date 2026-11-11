"""Tests for ``agent-foundry migrate quote-identifiers`` (issue #412)."""

from __future__ import annotations

from pathlib import Path

import click
from typer.testing import CliRunner

from agent_foundry.deploy.cli import app

# Match the existing test_cli.py click-version handling so the runner
# works on both click 8.1 and 8.2+.
_CLICK_MAJOR, _CLICK_MINOR, *_ = (int(p) for p in click.__version__.split(".")[:2])
_CLI_RUNNER_KWARGS: dict = {} if (_CLICK_MAJOR, _CLICK_MINOR) >= (8, 2) else {"mix_stderr": False}

UNQUOTED = "CREATE OR REPLACE VIEW ${var.catalog}.${var.schema}.my_view AS SELECT 1;\n"
QUOTED = "CREATE OR REPLACE VIEW `${var.catalog}`.`${var.schema}`.my_view AS SELECT 1;\n"


def _project(tmp_path: Path, body: str = UNQUOTED) -> Path:
    views = tmp_path / "instances" / "my-app" / "data_domains" / "docs" / "views"
    views.mkdir(parents=True)
    (views / "01_docs.sql").write_text(body, encoding="utf-8")
    return views / "01_docs.sql"


def _run(*args: str) -> tuple[int, str]:
    result = CliRunner(**_CLI_RUNNER_KWARGS).invoke(app, ["migrate", "quote-identifiers", *args])
    return result.exit_code, result.output


def test_help_smoke() -> None:
    code, output = _run("--help")

    assert code == 0, output
    assert "quote" in output.lower()


def test_rewrites_an_unquoted_template(tmp_path: Path) -> None:
    sql = _project(tmp_path)

    code, output = _run("--repo-root", str(tmp_path))

    assert code == 0, output
    assert sql.read_text(encoding="utf-8") == QUOTED
    assert "Rewrote 1 of 1" in output


def test_dry_run_reports_without_writing(tmp_path: Path) -> None:
    sql = _project(tmp_path)

    code, output = _run("--repo-root", str(tmp_path), "--dry-run")

    assert code == 0, output
    assert sql.read_text(encoding="utf-8") == UNQUOTED
    assert "[DRY RUN]" in output
    assert "1 of 1 file(s) would change" in output


def test_already_quoted_project_is_left_alone(tmp_path: Path) -> None:
    sql = _project(tmp_path, QUOTED)

    code, output = _run("--repo-root", str(tmp_path))

    assert code == 0, output
    assert sql.read_text(encoding="utf-8") == QUOTED
    assert "already quoted" in output


def test_scopes_to_one_instance(tmp_path: Path) -> None:
    first = _project(tmp_path)
    other = tmp_path / "instances" / "other-app" / "data_domains" / "d" / "views"
    other.mkdir(parents=True)
    second = other / "02.sql"
    second.write_text(UNQUOTED, encoding="utf-8")

    code, output = _run("--repo-root", str(tmp_path), "--instance", "my-app")

    assert code == 0, output
    assert first.read_text(encoding="utf-8") == QUOTED
    assert second.read_text(encoding="utf-8") == UNQUOTED


def test_empty_repo_is_not_an_error(tmp_path: Path) -> None:
    code, output = _run("--repo-root", str(tmp_path))

    assert code == 0, output
    assert "No .sql files found" in output
