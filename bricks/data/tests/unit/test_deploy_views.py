"""Tests for the data layer deploy script (offline)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_foundry.bricks import DestroyContext
from agent_foundry.destroy_base import NOTHING_TO_DESTROY
from agent_foundry.sql_text import split_sql_statements as _split_sql_statements
from agent_foundry.testing.fakes import FakeUcLookup, FakeWorkspaceClient
from agent_foundry.variables import substitute
from bricks.data.brick import DataDomainsBrick, qualified_schema
from bricks.data.scripts.deploy_views import (
    _collect_sql_files,
    deploy,
    destroy,
    load_domain_config,
)

# ---------------------------------------------------------------------------
# Offline tests (no workspace needed)
# ---------------------------------------------------------------------------


class TestSubstituteVariables:
    """Test variable substitution logic."""

    def test_simple_substitution(self) -> None:
        result = substitute(
            "CREATE VIEW ${var.catalog}.${var.schema}.v1",
            {"catalog": "main", "schema": "analytics"},
        )
        assert result == "CREATE VIEW main.analytics.v1"

    def test_repeated_variable(self) -> None:
        result = substitute(
            "${var.catalog}.${var.schema} and ${var.catalog}.${var.schema}",
            {"catalog": "main", "schema": "s"},
        )
        assert result == "main.s and main.s"

    def test_undefined_variable_raises(self) -> None:
        with pytest.raises(ValueError, match="Undefined variable.*missing"):
            substitute("${var.missing}", {})

    def test_no_variables_passthrough(self) -> None:
        sql = "SELECT 1"
        assert substitute(sql, {}) == sql

    def test_dollar_without_braces_ignored(self) -> None:
        sql = "SELECT $1 FROM t"
        assert substitute(sql, {}) == sql


class TestSplitSQLStatements:
    """Test SQL splitting on semicolons with comment stripping."""

    def test_splits_on_semicolons(self) -> None:
        raw = "SELECT 1; SELECT 2;"
        stmts = _split_sql_statements(raw, {})
        assert len(stmts) == 2
        assert stmts[0] == "SELECT 1"
        assert stmts[1] == "SELECT 2"

    def test_strips_comments(self) -> None:
        raw = "-- This is a comment\nSELECT 1;\n-- Another comment\nSELECT 2;"
        stmts = _split_sql_statements(raw, {})
        assert len(stmts) == 2

    def test_strips_empty_lines(self) -> None:
        raw = "\n\nSELECT 1;\n\n"
        stmts = _split_sql_statements(raw, {})
        assert len(stmts) == 1

    def test_substitutes_variables(self) -> None:
        raw = "SELECT * FROM ${var.catalog}.${var.schema}.t1;"
        stmts = _split_sql_statements(raw, {"catalog": "c", "schema": "s"})
        assert stmts[0] == "SELECT * FROM c.s.t1"

    def test_multiline_statement(self) -> None:
        raw = "COMMENT ON TABLE ${var.catalog}.t\nIS 'description';"
        stmts = _split_sql_statements(raw, {"catalog": "c"})
        assert len(stmts) == 1
        assert "COMMENT ON TABLE c.t" in stmts[0]
        assert "IS 'description'" in stmts[0]

    def test_metadata_file_multiple_statements(self, data_instance_dir: Path, data_variables: dict[str, str]) -> None:
        """Verify the template metadata file splits into expected number of statements."""
        metadata_file = data_instance_dir / "metadata" / "01_comments.sql"
        raw = metadata_file.read_text(encoding="utf-8")
        stmts = _split_sql_statements(raw, data_variables)
        assert len(stmts) >= 2

    def test_preserves_semicolon_inside_single_quoted_value(self) -> None:
        raw = "INSERT INTO catalog.schema.docs (id, content) VALUES\n  (1, 'First clause; second clause');"
        stmts = _split_sql_statements(raw, {})
        assert len(stmts) == 1
        assert "First clause; second clause" in stmts[0]

    def test_preserves_doubled_single_quote_escape(self) -> None:
        raw = "SELECT 'it''s; fine' AS a; SELECT 2;"
        stmts = _split_sql_statements(raw, {})
        assert len(stmts) == 2
        assert stmts[0] == "SELECT 'it''s; fine' AS a"
        assert stmts[1] == "SELECT 2"

    def test_preserves_semicolon_inside_double_quoted_identifier(self) -> None:
        raw = 'SELECT 1 AS "weird;name"; SELECT 2;'
        stmts = _split_sql_statements(raw, {})
        assert len(stmts) == 2
        assert stmts[0] == 'SELECT 1 AS "weird;name"'
        assert stmts[1] == "SELECT 2"

    def test_preserves_semicolon_inside_backtick_identifier(self) -> None:
        raw = "SELECT 1 AS `weird;name`; SELECT 2;"
        stmts = _split_sql_statements(raw, {})
        assert len(stmts) == 2
        assert stmts[0] == "SELECT 1 AS `weird;name`"
        assert stmts[1] == "SELECT 2"

    def test_strips_block_comment_and_ignores_semicolons_inside(self) -> None:
        raw = "SELECT 1 /* ignore ; this */; SELECT 2;"
        stmts = _split_sql_statements(raw, {})
        assert len(stmts) == 2
        assert stmts[0] == "SELECT 1"
        assert stmts[1] == "SELECT 2"

    def test_strips_inline_line_comment(self) -> None:
        raw = "SELECT 1 -- trailing ; comment\n; SELECT 2;"
        stmts = _split_sql_statements(raw, {})
        assert len(stmts) == 2
        assert stmts[0].startswith("SELECT 1")
        assert ";" not in stmts[0]
        assert stmts[1] == "SELECT 2"


class TestLoadDomainConfig:
    """Test domain.yml loading."""

    def test_loads_domain_name(self, data_instance_dir: Path) -> None:
        cfg = load_domain_config(data_instance_dir)
        assert cfg["domain"]["name"] == "catalog_explorer"

    def test_loads_schema_convention(self, data_instance_dir: Path) -> None:
        cfg = load_domain_config(data_instance_dir)
        assert cfg["domain"]["schema"] == "${var.schema_prefix}"

    def test_missing_domain_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_domain_config(tmp_path)


class TestCollectSQLFiles:
    """Test file discovery and ordering."""

    def test_discovers_data_files(self, data_instance_dir: Path) -> None:
        data_files, _ = _collect_sql_files(data_instance_dir)
        assert len(data_files) == 5  # 1 volume + 3 views + 1 table

    def test_volumes_before_views_before_tables_and_sorted(self, data_instance_dir: Path) -> None:
        data_files, _ = _collect_sql_files(data_instance_dir)
        volume_files = [f for f in data_files if "volumes" in f.parts]
        view_files = [f for f in data_files if "views" in f.parts]
        table_files = [f for f in data_files if "tables" in f.parts]
        assert data_files == volume_files + view_files + table_files
        assert volume_files == sorted(volume_files), "volumes should be sorted by path"
        assert view_files == sorted(view_files), "views should be sorted by path"
        assert table_files == sorted(table_files), "tables should be sorted by path"

    def test_discovers_metadata(self, data_instance_dir: Path) -> None:
        _, metadata = _collect_sql_files(data_instance_dir)
        assert len(metadata) == 2

    def test_empty_dir_returns_empty(self, tmp_path: Path) -> None:
        views, metadata = _collect_sql_files(tmp_path)
        assert views == []
        assert metadata == []


class TestDeployDryRun:
    """Test dry-run mode (no workspace, no API calls)."""

    def test_dry_run_returns_zero(self, data_instance_dir: Path, data_variables: dict[str, str]) -> None:
        rc = deploy(data_instance_dir, "dev", data_variables, dry_run=True)
        assert rc == 0

    def test_dry_run_with_missing_warehouse(self, data_instance_dir: Path) -> None:
        variables = {"catalog": "c", "schema": "s", "schema_prefix": "c_pfx"}
        rc = deploy(data_instance_dir, "dev", variables, dry_run=True)
        assert rc == 0  # warehouse not required for dry-run

    def test_deploy_requires_warehouse(self, data_instance_dir: Path) -> None:
        variables = {"catalog": "c", "schema": "s", "schema_prefix": "c_pfx"}
        rc = deploy(data_instance_dir, "dev", variables, dry_run=False)
        assert rc == 1  # fails because no warehouse_id

    def test_variable_substitution_in_sql(
        self,
        data_instance_dir: Path,
        data_variables: dict[str, str],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        deploy(data_instance_dir, "dev", data_variables, dry_run=True)
        output = capsys.readouterr().out
        assert "test_catalog.test_pfx" in output
        assert "unused_global_schema" not in output

    def test_domain_schema_override(self, data_instance_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """domain.yml schema should override the global schema variable."""
        variables = {
            "catalog": "my_cat",
            "schema": "global_schema",
            "schema_prefix": "my_pfx",
            "warehouse_id": "wh1",
        }
        rc = deploy(data_instance_dir, "dev", variables, dry_run=True)
        assert rc == 0
        output = capsys.readouterr().out
        assert "my_cat.my_pfx" in output
        assert "global_schema" not in output


class TestTagSchema:
    """Schema tagging in the data brick's deploy (SQL generation is tested in ``agent_foundry.sql_statements``)."""

    def test_dry_run_shows_tags(
        self,
        data_instance_dir: Path,
        data_variables: dict[str, str],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        data_variables["app_name"] = "test-app"
        deploy(data_instance_dir, "dev", data_variables, dry_run=True)
        output = capsys.readouterr().out
        assert "Would tag schema" in output
        assert "test-app" in output


# ---------------------------------------------------------------------------
# destroy() warehouse fallback
# ---------------------------------------------------------------------------


class TestDestroyWarehouseFallback:
    """``destroy()`` should auto-pick a RUNNING warehouse when the
    dedicated ``warehouse_id`` is missing (e.g. the warehouse brick
    already tore down its warehouse in a previous run).
    """

    def test_falls_back_to_running_warehouse(
        self,
        data_instance_dir: Path,
        data_variables: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        variables = {**data_variables}
        variables.pop("warehouse_id")

        executed: list[tuple[str, str]] = []

        import bricks.data.scripts.deploy_views as mod

        monkeypatch.setattr(mod, "_get_client", lambda variables, **_k: object())
        monkeypatch.setattr(
            "agent_foundry.destroy_base.pick_any_running_warehouse",
            lambda ws: "wh-fallback-123",
        )
        monkeypatch.setattr(
            "agent_foundry.sql_statements.execute_sql",
            lambda c, wh, sql: executed.append((wh, sql)),
        )

        rc = destroy(data_instance_dir, "dev", variables, force=True)
        assert rc == 0
        assert executed and all(wh == "wh-fallback-123" for wh, _ in executed)
        assert "DROP SCHEMA" in executed[0][1]
        assert "using running warehouse wh-fallback-123" in capsys.readouterr().out

    def test_no_running_warehouse_fails_loud(
        self,
        data_instance_dir: Path,
        data_variables: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        variables = {**data_variables}
        variables.pop("warehouse_id")

        import bricks.data.scripts.deploy_views as mod

        monkeypatch.setattr(mod, "_get_client", lambda variables, **_k: object())
        monkeypatch.setattr(
            "agent_foundry.destroy_base.pick_any_running_warehouse",
            lambda ws: None,
        )

        rc = destroy(data_instance_dir, "dev", variables, force=True)
        assert rc == 1
        assert "no RUNNING warehouse available" in capsys.readouterr().out


def _no_warehouse_lookup(ws: Any) -> str:
    raise AssertionError("destroy looked for a warehouse with nothing to drop")


class TestBrickDestroyChecksTheSchema:
    """The brick drops the schema only when Unity Catalog has it (#542)."""

    def test_missing_schema_is_skipped_without_a_warehouse(
        self, data_instance_dir: Path, data_variables: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ws = FakeWorkspaceClient()
        monkeypatch.setattr(ws, "schemas", FakeUcLookup(), raising=False)
        monkeypatch.setattr("agent_foundry.destroy_base.pick_any_running_warehouse", _no_warehouse_lookup)
        variables = {k: v for k, v in data_variables.items() if k != "warehouse_id"}
        ctx = DestroyContext(
            instance_dir=data_instance_dir.parents[1], target="dev", variables=variables, ws=ws, force=True
        )

        results = DataDomainsBrick().destroy(ctx)

        assert [(r.status, r.details) for r in results] == [("skipped", {"reason": NOTHING_TO_DESTROY})]

    def test_existing_schema_is_dropped(
        self, data_instance_dir: Path, data_variables: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        executed: list[str] = []
        ws = FakeWorkspaceClient()
        monkeypatch.setattr(
            ws, "schemas", FakeUcLookup(qualified_schema(data_instance_dir, data_variables)), raising=False
        )
        monkeypatch.setattr("agent_foundry.sql_statements.execute_sql", lambda c, wh, sql: executed.append(sql))
        ctx = DestroyContext(
            instance_dir=data_instance_dir.parents[1], target="dev", variables=data_variables, ws=ws, force=True
        )

        results = DataDomainsBrick().destroy(ctx)

        assert [r.status for r in results] == ["ok"]
        assert executed and "DROP SCHEMA" in executed[0]


class TestMainResolvesTheInstance:
    """``--instance`` used to resolve to ``instances/data_domains/<app>``.

    That directory never exists in the real layout
    (``instances/<app>/data_domains/<domain>``), so every CLI invocation
    failed with "Instance directory not found".
    """

    @staticmethod
    def _project(tmp_path: Path) -> Path:
        domains = tmp_path / "instances" / "my-app" / "data_domains"
        for name in ("alpha", "beta"):
            (domains / name).mkdir(parents=True)
            (domains / name / "domain.yml").write_text("domain:\n  schema: s\n", encoding="utf-8")
        (domains / "no_config").mkdir()
        return tmp_path

    def _run(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        instance: str,
    ) -> tuple[int, list[Path]]:
        import bricks.data.scripts.deploy_views as mod

        deployed: list[Path] = []

        def fake_deploy(domain_dir: Path, target: str, variables: dict[str, str], dry_run: bool, **_k: object) -> int:
            deployed.append(domain_dir)
            return 0

        monkeypatch.setattr(mod, "load_brick_variables", lambda *a, **k: {})
        monkeypatch.setattr(mod, "deploy", fake_deploy)

        rc = mod.main(["--instance", instance, "--project-root", str(self._project(tmp_path))])
        return rc, deployed

    def test_app_name_deploys_every_configured_domain(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        rc, deployed = self._run(tmp_path, monkeypatch, "my-app")

        assert rc == 0
        assert [d.name for d in deployed] == ["alpha", "beta"], "a domain without domain.yml is skipped"

    def test_app_and_domain_pair_deploys_one(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        rc, deployed = self._run(tmp_path, monkeypatch, "my-app/beta")

        assert rc == 0
        assert [d.name for d in deployed] == ["beta"]

    def test_unknown_instance_reports_an_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc, deployed = self._run(tmp_path, monkeypatch, "does-not-exist")

        assert rc == 1
        assert not deployed
        assert "Instance directory not found" in capsys.readouterr().err

    def test_variables_are_loaded_for_the_owning_instance(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``load_brick_variables`` keys off the basename, so it needs the app dir."""
        import bricks.data.scripts.deploy_views as mod

        seen: list[Path] = []

        def _record_vars(root: object, target: object, d: Path, **k: object) -> dict[str, str]:
            seen.append(d)
            return {}

        monkeypatch.setattr(mod, "load_brick_variables", _record_vars)
        monkeypatch.setattr(mod, "deploy", lambda *a, **k: 0)

        mod.main(["--instance", "my-app", "--project-root", str(self._project(tmp_path))])

        assert [d.name for d in seen] == ["my-app"]
