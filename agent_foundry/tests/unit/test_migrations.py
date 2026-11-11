"""Tests for the in-place project rewrites.

The rules mirror ``bricks/data/tests/unit/test_sql_identifier_quoting.py``, which is
the spec: what the guard rejects in shipped templates, this rewrite fixes in a
project's own copies.
"""

from __future__ import annotations

from pathlib import Path

from agent_foundry.migrations import (
    apply_rewrites,
    find_project_sql,
    plan_quote_identifiers,
    quote_identifiers,
)


class TestQuoteIdentifiers:
    def test_quotes_a_view_target(self) -> None:
        rewritten = quote_identifiers("CREATE OR REPLACE VIEW ${var.catalog}.${var.schema}.my_view AS SELECT 1;")

        assert rewritten == "CREATE OR REPLACE VIEW `${var.catalog}`.`${var.schema}`.my_view AS SELECT 1;"

    def test_quotes_a_from_clause(self) -> None:
        rewritten = quote_identifiers("  FROM ${var.catalog}.${var.schema}.docs\n")

        assert rewritten == "  FROM `${var.catalog}`.`${var.schema}`.docs\n"

    def test_replaces_the_two_part_agent_schema(self) -> None:
        rewritten = quote_identifiers("CREATE OR REPLACE FUNCTION ${var.agent_schema}.get_landscape()")

        assert rewritten == "CREATE OR REPLACE FUNCTION `${var.catalog}`.`${var.agent_schema_name}`.get_landscape()"

    def test_replaces_a_wrongly_quoted_agent_schema(self) -> None:
        """`` `cat.sch` `` names one schema called "cat.sch", so it needs the same fix."""
        rewritten = quote_identifiers("FROM `${var.agent_schema}`.landscape_views")

        assert rewritten == "FROM `${var.catalog}`.`${var.agent_schema_name}`.landscape_views"

    def test_is_idempotent(self) -> None:
        once = quote_identifiers("CREATE TABLE ${var.catalog}.${var.schema}.t (a INT);")

        assert quote_identifiers(once) == once

    def test_leaves_comments_alone(self) -> None:
        line = "-- writes to ${var.catalog}.${var.schema}.docs"

        assert quote_identifiers(line) == line

    def test_leaves_volume_paths_bare(self) -> None:
        """A volume path is a string literal; a back tick there becomes part of the path."""
        line = "  '/Volumes/${var.catalog}/${var.schema}/source_docs/'"

        assert quote_identifiers(line) == line

    def test_preserves_trailing_newline_and_blank_lines(self) -> None:
        text = "SELECT 1;\n\nSELECT 2;\n"

        assert quote_identifiers(text) == text


class TestMetricViewYamlSource:
    def test_quotes_the_name_inside_a_yaml_scalar(self) -> None:
        rewritten = quote_identifiers("  source: ${var.catalog}.${var.schema}.catalog_objects")

        assert rewritten == '  source: "`${var.catalog}`.`${var.schema}`.catalog_objects"'

    def test_leaves_an_already_quoted_scalar_alone(self) -> None:
        line = '  source: "`${var.catalog}`.`${var.schema}`.catalog_objects"'

        assert quote_identifiers(line) == line

    def test_does_not_add_quotes_to_a_plain_name(self) -> None:
        """No back ticks means nothing to protect from the YAML parser."""
        line = "  source: catalog_objects"

        assert quote_identifiers(line) == line


class TestDiscovery:
    def test_finds_project_sql_and_skips_build_output(self, tmp_path: Path) -> None:
        domain = tmp_path / "instances" / "my-app" / "data_domains" / "docs" / "views"
        domain.mkdir(parents=True)
        (domain / "01_docs.sql").write_text("SELECT 1;", encoding="utf-8")
        build = tmp_path / "instances" / "my-app" / "genie_spaces" / "s" / "_build"
        build.mkdir(parents=True)
        (build / "generated.sql").write_text("SELECT 2;", encoding="utf-8")

        found = find_project_sql(tmp_path)

        assert [p.name for p in found] == ["01_docs.sql"]

    def test_scopes_to_one_instance(self, tmp_path: Path) -> None:
        for app in ("app-a", "app-b"):
            path = tmp_path / "instances" / app / "data_domains" / "d" / "views"
            path.mkdir(parents=True)
            (path / f"{app}.sql").write_text("SELECT 1;", encoding="utf-8")

        found = find_project_sql(tmp_path, "app-a")

        assert [p.name for p in found] == ["app-a.sql"]

    def test_missing_instances_directory_is_not_an_error(self, tmp_path: Path) -> None:
        assert find_project_sql(tmp_path) == []


class TestApply:
    def test_writes_only_the_changed_files(self, tmp_path: Path) -> None:
        stale = tmp_path / "stale.sql"
        stale.write_text("SELECT * FROM ${var.catalog}.${var.schema}.t;\n", encoding="utf-8")
        clean = tmp_path / "clean.sql"
        clean.write_text("SELECT * FROM `${var.catalog}`.`${var.schema}`.t;\n", encoding="utf-8")
        clean_mtime = clean.stat().st_mtime_ns

        rewrites = plan_quote_identifiers([stale, clean])
        apply_rewrites(rewrites)

        assert stale.read_text(encoding="utf-8") == "SELECT * FROM `${var.catalog}`.`${var.schema}`.t;\n"
        assert clean.stat().st_mtime_ns == clean_mtime

    def test_reports_the_changed_line_numbers(self, tmp_path: Path) -> None:
        path = tmp_path / "multi.sql"
        path.write_text(
            "-- header\nCREATE VIEW ${var.catalog}.${var.schema}.v AS\n  SELECT 1;\n",
            encoding="utf-8",
        )

        rewrite = plan_quote_identifiers([path])[0]

        assert rewrite.changed
        assert rewrite.changed_line_numbers == [2]
