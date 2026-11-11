"""Tests for the SQL function deploy script (offline)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_foundry.bricks import DestroyContext
from agent_foundry.destroy_base import NOTHING_TO_DESTROY
from agent_foundry.sql_text import split_sql_statements as _split_sql_statements
from agent_foundry.state import save_state
from agent_foundry.testing.fakes import FakeUcLookup, FakeWorkspaceClient
from bricks.sql_function.brick import SqlFunctionBrick
from bricks.sql_function.scripts.deploy_functions import (
    _check_comment_clause,
    _collect_sql_files,
    _extract_function_names,
    _resolve_schema_parts,
    _save_state,
    deploy,
    destroy,
    functions_to_drop,
    load_function_config,
    load_state,
)

# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


class TestLoadFunctionConfig:
    """Test config.yml loading."""

    def test_loads_config(self, fn_instance_dir: Path) -> None:
        cfg = load_function_config(fn_instance_dir)
        assert isinstance(cfg, dict)

    def test_missing_config_returns_empty(self, tmp_path: Path) -> None:
        cfg = load_function_config(tmp_path)
        assert cfg == {}


# ---------------------------------------------------------------------------
# Schema resolution
# ---------------------------------------------------------------------------


class TestResolveSchema:
    """Test catalog/schema resolution from config and variables."""

    def test_defaults_to_agent_schema(self, fn_variables: dict[str, str]) -> None:
        catalog, schema = _resolve_schema_parts({}, fn_variables)
        assert catalog == "test_catalog"
        assert schema == "test_app_agent"

    def test_config_override(self, fn_variables: dict[str, str]) -> None:
        config = {"catalog": "custom_cat", "schema": "custom_schema"}
        catalog, schema = _resolve_schema_parts(config, fn_variables)
        assert catalog == "custom_cat"
        assert schema == "custom_schema"

    def test_variable_substitution_in_config(self, fn_variables: dict[str, str]) -> None:
        config = {"schema": "${var.schema_prefix}_functions"}
        catalog, schema = _resolve_schema_parts(config, fn_variables)
        assert schema == "test_app_functions"

    def test_strips_catalog_from_dotted_schema(self, fn_variables: dict[str, str]) -> None:
        config = {"schema": "some_cat.some_schema"}
        catalog, schema = _resolve_schema_parts(config, fn_variables)
        assert schema == "some_schema"

    def test_fallback_without_agent_schema(self) -> None:
        variables = {"catalog": "cat", "schema_prefix": "pfx"}
        catalog, schema = _resolve_schema_parts({}, variables)
        assert catalog == "cat"
        assert schema == "pfx_agent"


# ---------------------------------------------------------------------------
# SQL splitting
# ---------------------------------------------------------------------------


class TestSplitSQLStatements:
    """Test SQL splitting on semicolons with comment stripping."""

    def test_splits_on_semicolons(self) -> None:
        raw = "SELECT 1; SELECT 2;"
        stmts = _split_sql_statements(raw, {})
        assert len(stmts) == 2
        assert stmts[0] == "SELECT 1"
        assert stmts[1] == "SELECT 2"

    def test_strips_comments(self) -> None:
        raw = "-- comment\nSELECT 1;\n-- another\nSELECT 2;"
        stmts = _split_sql_statements(raw, {})
        assert len(stmts) == 2

    def test_strips_empty_lines(self) -> None:
        raw = "\n\nSELECT 1;\n\n"
        stmts = _split_sql_statements(raw, {})
        assert len(stmts) == 1

    def test_substitutes_variables(self) -> None:
        raw = "SELECT * FROM ${var.agent_schema}.t1;"
        stmts = _split_sql_statements(
            raw,
            {"agent_schema": "cat.sch"},
        )
        assert stmts[0] == "SELECT * FROM cat.sch.t1"

    def test_preserves_semicolon_inside_single_quoted_comment(self) -> None:
        raw = (
            "CREATE OR REPLACE FUNCTION s.f(x STRING)\n"
            "RETURNS STRING\n"
            "COMMENT 'Does the thing (special case); call when y is set'\n"
            "LANGUAGE SQL\n"
            "RETURN x;\n"
        )
        stmts = _split_sql_statements(raw, {})
        assert len(stmts) == 1
        assert "(special case); call when y is set" in stmts[0]

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


# ---------------------------------------------------------------------------
# File collection
# ---------------------------------------------------------------------------


class TestCollectSQLFiles:
    """Test file discovery and ordering."""

    def test_discovers_sql_files(self, fn_instance_dir: Path) -> None:
        files = _collect_sql_files(fn_instance_dir)
        assert len(files) >= 1
        assert all(f.suffix == ".sql" for f in files)

    def test_sorted_alphabetically(self, fn_instance_dir: Path) -> None:
        files = _collect_sql_files(fn_instance_dir)
        assert files == sorted(files)

    def test_empty_dir_returns_empty(self, tmp_path: Path) -> None:
        assert _collect_sql_files(tmp_path) == []


# ---------------------------------------------------------------------------
# COMMENT clause check
# ---------------------------------------------------------------------------


class TestCheckCommentClause:
    """Test COMMENT clause detection."""

    def test_no_warning_when_comment_present(self, tmp_path: Path) -> None:
        sql = "CREATE FUNCTION f() RETURNS INT COMMENT 'desc' RETURN 1;"
        f = tmp_path / "test.sql"
        f.write_text(sql)
        assert _check_comment_clause(f, sql) == []

    def test_warning_when_comment_missing(self, tmp_path: Path) -> None:
        sql = "CREATE FUNCTION f() RETURNS INT RETURN 1;"
        f = tmp_path / "test.sql"
        f.write_text(sql)
        warnings = _check_comment_clause(f, sql)
        assert len(warnings) == 1
        assert "no COMMENT clause" in warnings[0]


# ---------------------------------------------------------------------------
# Function name extraction
# ---------------------------------------------------------------------------


class TestExtractFunctionNames:
    """Test function name extraction from SQL files."""

    def test_extracts_names(self, fn_instance_dir: Path, fn_variables: dict[str, str]) -> None:
        files = _collect_sql_files(fn_instance_dir)
        names = _extract_function_names(files, fn_variables)
        assert len(names) >= 1
        assert any("example_fn" in n for n in names)

    def test_three_level_namespace(self, fn_instance_dir: Path, fn_variables: dict[str, str]) -> None:
        files = _collect_sql_files(fn_instance_dir)
        names = _extract_function_names(files, fn_variables)
        for name in names:
            parts = name.split(".")
            assert len(parts) == 3, f"Expected three-level name, got: {name}"

    def test_or_replace_variant(self, tmp_path: Path) -> None:
        sql = "CREATE OR REPLACE FUNCTION cat.sch.my_fn() RETURNS INT RETURN 1;"
        f = tmp_path / "test.sql"
        f.write_text(sql)
        names = _extract_function_names([f], {})
        assert names == ["cat.sch.my_fn"]

    def test_hyphenated_names_are_not_truncated(self, tmp_path: Path) -> None:
        """A truncated name poisons the state file and the destroy path."""
        f = tmp_path / "hyphen.sql"
        f.write_text("CREATE OR REPLACE FUNCTION `cat`.`ms-eu-dev-agent`.my_fn() RETURNS INT RETURN 1;")

        assert _extract_function_names([f], {}) == ["cat.ms-eu-dev-agent.my_fn"]

    def test_the_quoted_template_form_resolves_to_a_clean_name(self, tmp_path: Path) -> None:
        """Templates back-quote each part; state and DROP statements want it bare."""
        f = tmp_path / "quoted.sql"
        f.write_text(
            "CREATE OR REPLACE FUNCTION `${var.catalog}`.`${var.agent_schema_name}`.fn() RETURNS INT RETURN 1;"
        )

        names = _extract_function_names([f], {"catalog": "cat", "agent_schema_name": "sch_agent"})

        assert names == ["cat.sch_agent.fn"]


# ---------------------------------------------------------------------------
# Workspace state
# ---------------------------------------------------------------------------


def _deploy_with_fake_ws(
    fn_instance_dir: Path, fn_variables: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> FakeWorkspaceClient:
    """Real deploy against an in-memory workspace; SQL execution is stubbed, the grant skipped."""
    monkeypatch.setattr("agent_foundry.sql_statements.execute_sql", lambda c, wh, sql: {})
    ws = FakeWorkspaceClient()
    variables = {k: v for k, v in fn_variables.items() if k != "app_name"}
    assert deploy(fn_instance_dir, "dev", variables, ws=ws) == 0
    return ws


class TestWorkspaceState:
    """Deployed function names live in the shared workspace state, not in the checkout."""

    def test_deploy_saves_function_names_to_workspace(
        self, fn_instance_dir: Path, fn_variables: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ws = _deploy_with_fake_ws(fn_instance_dir, fn_variables, monkeypatch)
        state = load_state(ws, fn_instance_dir, "dev")
        assert state["deployed_functions"]
        assert all(name.startswith("test_catalog.test_app_agent.") for name in state["deployed_functions"])
        assert not (fn_instance_dir / "_state").exists()

    def test_destroy_reads_and_clears_workspace_state(
        self, fn_instance_dir: Path, fn_variables: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ws = _deploy_with_fake_ws(fn_instance_dir, fn_variables, monkeypatch)
        deployed = load_state(ws, fn_instance_dir, "dev")["deployed_functions"]
        monkeypatch.setattr(ws, "functions", FakeUcLookup(*deployed), raising=False)
        assert functions_to_drop(fn_instance_dir, "dev", fn_variables, ws=ws) == deployed

        assert destroy(fn_instance_dir, "dev", fn_variables, force=True, ws=ws) == 0
        assert load_state(ws, fn_instance_dir, "dev") == {}


def _no_warehouse_lookup(ws: Any) -> str:
    raise AssertionError("destroy looked for a warehouse with nothing to drop")


class TestDestroyMissingFunctions:
    """Destroy drops only what Unity Catalog still has (#542)."""

    def test_functions_gone_from_uc_are_left_out(
        self, fn_instance_dir: Path, fn_variables: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ws = FakeWorkspaceClient()
        _save_state(ws, fn_instance_dir, "dev", ["cat.sch.kept", "cat.sch.gone"], deployer="test")
        monkeypatch.setattr(ws, "functions", FakeUcLookup("cat.sch.kept"), raising=False)

        assert functions_to_drop(fn_instance_dir, "dev", fn_variables, ws=ws) == ["cat.sch.kept"]

    def test_stale_state_is_cleared_without_a_warehouse(
        self, fn_instance_dir: Path, fn_variables: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ws = FakeWorkspaceClient()
        _save_state(ws, fn_instance_dir, "dev", ["cat.sch.gone"], deployer="test")
        monkeypatch.setattr(ws, "functions", FakeUcLookup(), raising=False)
        monkeypatch.setattr("agent_foundry.destroy_base.pick_any_running_warehouse", _no_warehouse_lookup)
        variables = {k: v for k, v in fn_variables.items() if k != "warehouse_id"}

        assert destroy(fn_instance_dir, "dev", variables, force=True, ws=ws) == 0
        assert load_state(ws, fn_instance_dir, "dev") == {}

    def test_never_deployed_scaffold_is_skipped(
        self, fn_instance_dir: Path, fn_variables: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ws = FakeWorkspaceClient()
        monkeypatch.setattr(ws, "functions", FakeUcLookup(), raising=False)
        monkeypatch.setattr("agent_foundry.destroy_base.pick_any_running_warehouse", _no_warehouse_lookup)
        variables = {k: v for k, v in fn_variables.items() if k != "warehouse_id"}
        ctx = DestroyContext(
            instance_dir=fn_instance_dir.parents[1], target="dev", variables=variables, ws=ws, force=True
        )

        results = SqlFunctionBrick().destroy(ctx)

        assert [(r.status, r.details) for r in results] == [("skipped", {"reason": NOTHING_TO_DESTROY})]


# ---------------------------------------------------------------------------
# Deploy (dry-run, no workspace)
# ---------------------------------------------------------------------------


class TestDeployDryRun:
    """Test dry-run mode (no workspace, no API calls)."""

    def test_dry_run_returns_zero(
        self,
        fn_instance_dir: Path,
        fn_variables: dict[str, str],
    ) -> None:
        rc = deploy(fn_instance_dir, "dev", fn_variables, dry_run=True)
        assert rc == 0

    def test_dry_run_without_warehouse(
        self,
        fn_instance_dir: Path,
        fn_variables: dict[str, str],
    ) -> None:
        variables = {**fn_variables}
        del variables["warehouse_id"]
        rc = deploy(fn_instance_dir, "dev", variables, dry_run=True)
        assert rc == 0

    def test_deploy_requires_warehouse(
        self,
        fn_instance_dir: Path,
        fn_variables: dict[str, str],
    ) -> None:
        variables = {**fn_variables}
        del variables["warehouse_id"]
        rc = deploy(fn_instance_dir, "dev", variables, dry_run=False)
        assert rc == 1

    def test_variable_substitution_in_dry_run(
        self,
        fn_instance_dir: Path,
        fn_variables: dict[str, str],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        deploy(fn_instance_dir, "dev", fn_variables, dry_run=True)
        output = capsys.readouterr().out
        assert "test_catalog.test_app_agent" in output

    def test_dry_run_writes_no_state(
        self,
        fn_instance_dir: Path,
        fn_variables: dict[str, str],
    ) -> None:
        ws = FakeWorkspaceClient()
        deploy(fn_instance_dir, "dev", fn_variables, dry_run=True, ws=ws)
        assert not (fn_instance_dir / "_state").exists()
        assert ws.workspace.files == {}

    def test_empty_functions_dir_skips(
        self,
        fn_instance_dir: Path,
        fn_variables: dict[str, str],
    ) -> None:
        import shutil

        shutil.rmtree(fn_instance_dir / "functions")
        (fn_instance_dir / "functions").mkdir()
        rc = deploy(fn_instance_dir, "dev", fn_variables, dry_run=True)
        assert rc == 0

    def test_dry_run_shows_grant_intent(
        self,
        fn_instance_dir: Path,
        fn_variables: dict[str, str],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        deploy(fn_instance_dir, "dev", fn_variables, dry_run=True)
        output = capsys.readouterr().out
        assert "GRANT USE SCHEMA" in output

    def test_comment_warning_in_dry_run(
        self,
        fn_instance_dir: Path,
        fn_variables: dict[str, str],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        no_comment = fn_instance_dir / "functions" / "no_comment.sql"
        no_comment.write_text(
            "CREATE OR REPLACE FUNCTION ${var.agent_schema}.bare_fn()\nRETURNS INT\nLANGUAGE SQL\nRETURN 42;\n"
        )
        deploy(fn_instance_dir, "dev", fn_variables, dry_run=True)
        output = capsys.readouterr().out
        assert "no COMMENT clause" in output


class TestGrantUseSchemaToAppAppMissing:
    """``_grant_use_schema_to_app`` is called during every SQL-function
    deploy.  On the first deploy the app does not yet exist, so the
    grant has to be deferred to the agent_app brick.  That's expected
    ordering -- it must NOT emit a scary WARNING and must NOT raise.
    """

    def test_app_not_found_is_quiet(self, capsys: pytest.CaptureFixture[str]) -> None:
        from unittest.mock import MagicMock

        from bricks.sql_function.scripts.deploy_functions import (
            _grant_use_schema,
        )

        client = MagicMock()
        client.apps.get.side_effect = Exception("App with name my-test-app does not exist or is deleted.")
        _grant_use_schema(
            client,
            "wh-x",
            "main",
            "sch",
            "my-test-app",
        )
        output = capsys.readouterr().out
        assert "WARNING" not in output
        assert "not yet deployed" in output
        assert "Agent App brick" in output

    def test_other_errors_still_warn(self, capsys: pytest.CaptureFixture[str]) -> None:
        from unittest.mock import MagicMock

        from bricks.sql_function.scripts.deploy_functions import (
            _grant_use_schema,
        )

        client = MagicMock()
        client.apps.get.side_effect = Exception(
            "Permission denied: missing apps.read permission",
        )
        _grant_use_schema(
            client,
            "wh-x",
            "main",
            "sch",
            "my-test-app",
        )
        output = capsys.readouterr().out
        assert "WARNING" in output
        assert "Permission denied" in output


# ---------------------------------------------------------------------------
# destroy() warehouse fallback
# ---------------------------------------------------------------------------


class TestDestroyWarehouseFallback:
    """``destroy()`` should auto-pick a RUNNING warehouse when the
    dedicated ``warehouse_id`` is missing from variables (typical
    after the warehouse brick has already torn down its own warehouse
    in a previous run).
    """

    def _seed_state(self, instance_dir: Path) -> None:
        state_dir = instance_dir / "_state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "dev.json").write_text(
            '{"deployed_functions": ["test_catalog.test_schema.my_fn"]}',
            encoding="utf-8",
        )

    def test_falls_back_to_running_warehouse(
        self,
        fn_instance_dir: Path,
        fn_variables: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        self._seed_state(fn_instance_dir)
        variables = {**fn_variables}
        variables.pop("warehouse_id")

        executed: list[tuple[str, str]] = []

        class _Client:
            warehouses = type("WH", (), {"list": lambda self: iter([])})()

        client = _Client()

        import bricks.sql_function.scripts.deploy_functions as mod

        monkeypatch.setattr(mod, "_get_client", lambda variables, **_k: client)
        monkeypatch.setattr(
            "agent_foundry.destroy_base.pick_any_running_warehouse",
            lambda ws: "wh-fallback-123",
        )
        monkeypatch.setattr(
            "agent_foundry.sql_statements.execute_sql",
            lambda c, wh, sql: executed.append((wh, sql)),
        )

        rc = destroy(fn_instance_dir, "dev", variables, force=True)
        assert rc == 0
        assert executed and all(wh == "wh-fallback-123" for wh, _ in executed)
        assert "using running warehouse wh-fallback-123" in capsys.readouterr().out

    def test_no_running_warehouse_fails_loud(
        self,
        fn_instance_dir: Path,
        fn_variables: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        self._seed_state(fn_instance_dir)
        variables = {**fn_variables}
        variables.pop("warehouse_id")

        import bricks.sql_function.scripts.deploy_functions as mod

        monkeypatch.setattr(mod, "_get_client", lambda variables, **_k: object())
        monkeypatch.setattr(
            "agent_foundry.destroy_base.pick_any_running_warehouse",
            lambda ws: None,
        )

        rc = destroy(fn_instance_dir, "dev", variables, force=True)
        assert rc == 1
        assert "no RUNNING warehouse available" in capsys.readouterr().out


class TestDestroyDropsEmptySchema:
    """``destroy()`` must attempt ``DROP SCHEMA ... RESTRICT`` after
    dropping the functions, so it cleans up the schema sql_function
    created during deploy.  RESTRICT (not CASCADE) -- preserves other
    tables/functions in the schema.
    """

    def _seed_state(
        self, instance_dir: Path, functions: list[str], monkeypatch: pytest.MonkeyPatch
    ) -> FakeWorkspaceClient:
        """Workspace whose state and Unity Catalog both know *functions*."""
        ws = FakeWorkspaceClient()
        app, sub = instance_dir.parent.parent.name, instance_dir.name
        save_state(ws, app, "sql_function", sub, "dev", {"deployed_functions": functions})
        monkeypatch.setattr(ws, "functions", FakeUcLookup(*functions), raising=False)
        return ws

    def test_drops_schema_after_functions(
        self,
        fn_instance_dir: Path,
        fn_variables: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ws = self._seed_state(
            fn_instance_dir, ["test_catalog.test_schema.fn_a", "test_catalog.test_schema.fn_b"], monkeypatch
        )

        executed: list[str] = []
        import bricks.sql_function.scripts.deploy_functions as mod

        monkeypatch.setattr(mod, "_get_client", lambda variables, **_k: object())
        monkeypatch.setattr("agent_foundry.sql_statements.execute_sql", lambda c, wh, sql: executed.append(sql))

        rc = destroy(fn_instance_dir, "dev", fn_variables, force=True, ws=ws)
        assert rc == 0

        drops_fn = [s for s in executed if s.startswith("DROP FUNCTION")]
        drops_schema = [s for s in executed if s.startswith("DROP SCHEMA")]
        assert len(drops_fn) == 2
        assert len(drops_schema) == 1, f"Expected one DROP SCHEMA, got: {executed}"
        assert "RESTRICT" in drops_schema[0], "Must be RESTRICT, not CASCADE"
        assert "`test_catalog`.`test_schema`" in drops_schema[0]

    def test_preserves_non_empty_schema(
        self,
        fn_instance_dir: Path,
        fn_variables: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """When other tables/functions live in the schema, RESTRICT
        fails and we log a friendly preserved message instead of
        failing the destroy."""
        ws = self._seed_state(fn_instance_dir, ["test_catalog.test_schema.fn_a"], monkeypatch)

        import bricks.sql_function.scripts.deploy_functions as mod

        def _stub_execute(client: object, wh: str, sql: str) -> None:  # noqa: ARG001
            if sql.startswith("DROP SCHEMA"):
                raise RuntimeError("Schema test_catalog.test_schema is not empty (RESTRICT)")

        monkeypatch.setattr(mod, "_get_client", lambda variables, **_k: object())
        monkeypatch.setattr("agent_foundry.sql_statements.execute_sql", _stub_execute)

        rc = destroy(fn_instance_dir, "dev", fn_variables, force=True, ws=ws)
        assert rc == 0
        out = capsys.readouterr().out
        assert "not empty" in out
        assert "preserved" in out

    def test_skips_schema_drop_when_functions_span_multiple_schemas(
        self,
        fn_instance_dir: Path,
        fn_variables: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Safety: if functions live across more than one schema,
        we can't pick one to drop -- skip entirely."""
        ws = self._seed_state(
            fn_instance_dir, ["test_catalog.schema_a.fn_a", "test_catalog.schema_b.fn_b"], monkeypatch
        )

        executed: list[str] = []
        import bricks.sql_function.scripts.deploy_functions as mod

        monkeypatch.setattr(mod, "_get_client", lambda variables, **_k: object())
        monkeypatch.setattr("agent_foundry.sql_statements.execute_sql", lambda c, wh, sql: executed.append(sql))

        rc = destroy(fn_instance_dir, "dev", fn_variables, force=True, ws=ws)
        assert rc == 0
        assert not any(s.startswith("DROP SCHEMA") for s in executed), (
            f"Must not drop a schema when functions span multiple schemas. Got: {executed}"
        )


class TestSharedSplitter:
    """This brick's splitter used to lack backtick tracking (see agent_foundry.sql_text)."""

    def test_semicolon_inside_a_quoted_identifier_does_not_split(self) -> None:
        raw = "CREATE OR REPLACE FUNCTION `cat`.`odd;schema`.fn() RETURNS INT RETURN 1;"

        assert _split_sql_statements(raw, {}) == [
            "CREATE OR REPLACE FUNCTION `cat`.`odd;schema`.fn() RETURNS INT RETURN 1"
        ]

    def test_comment_marker_inside_a_quoted_identifier_is_kept(self) -> None:
        statements = _split_sql_statements("SELECT * FROM `a--b`.t;", {})

        assert statements == ["SELECT * FROM `a--b`.t"]


class TestMainResolvesTheInstance:
    """``--instance`` used to resolve to ``instances/sql_functions/<app>``, which never exists."""

    @staticmethod
    def _project(tmp_path: Path) -> Path:
        groups = tmp_path / "instances" / "my-app" / "sql_functions"
        for name in ("catalog_utils", "data_universe"):
            (groups / name / "functions").mkdir(parents=True)
        return tmp_path

    def test_app_name_deploys_every_group(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import bricks.sql_function.scripts.deploy_functions as mod

        deployed: list[Path] = []

        def _record_deploy(d: Path, *a: object, **k: object) -> int:
            deployed.append(d)
            return 0

        monkeypatch.setattr(mod, "load_brick_variables", lambda *a, **k: {})
        monkeypatch.setattr(mod, "deploy", _record_deploy)

        rc = mod.main(["--instance", "my-app", "--project-root", str(self._project(tmp_path))])

        assert rc == 0
        assert [d.name for d in deployed] == ["catalog_utils", "data_universe"]

    def test_variables_are_loaded_for_the_owning_instance(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import bricks.sql_function.scripts.deploy_functions as mod

        seen: list[Path] = []

        def _record_vars(root: object, target: object, d: Path, **k: object) -> dict[str, str]:
            seen.append(d)
            return {}

        monkeypatch.setattr(mod, "load_brick_variables", _record_vars)
        monkeypatch.setattr(mod, "deploy", lambda *a, **k: 0)

        mod.main(["--instance", "my-app", "--project-root", str(self._project(tmp_path))])

        assert [d.name for d in seen] == ["my-app"]
