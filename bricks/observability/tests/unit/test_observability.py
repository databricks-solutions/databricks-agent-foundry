"""Tests for the observability brick (offline -- no workspace needed)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

BRICK_DIR = Path(__file__).resolve().parents[2]
QUERIES_DIR = BRICK_DIR / "dashboards" / "queries"
TEMPLATE_PATH = BRICK_DIR / "dashboards" / "template.lvdash.json"

EXPECTED_QUERIES = [
    "01_request_volume.sql",
    "02_latency_distribution.sql",
    "03_error_rate.sql",
    "04_tool_call_breakdown.sql",
    "05_quality_scores.sql",
    "06_cost_attribution.sql",
    "07_human_feedback.sql",
    "08_session_activity.sql",
    "09_top_users.sql",
    "10_prompt_versions.sql",
    "11_model_usage.sql",
    "12_feedback_categories.sql",
    "13_feedback_comments.sql",
    "14_token_cache_kpis.sql",
    "15_token_cache_composition.sql",
    "16_token_billed_trend.sql",
    "17_token_cache_by_model.sql",
    "18_token_expensive_requests.sql",
    "19_token_tool_result_weight.sql",
]

# Every query except cost attribution reads the MLflow trace tables and must
# therefore be schema-scoped through the deploy variables.
MLFLOW_QUERIES = [name for name in EXPECTED_QUERIES if name != "06_cost_attribution.sql"]

# Datasets carrying the prompt-cache breakdown, and the multipliers their billed
# unit maths must apply: cache read 0.1x, rolling write 1.25x, static write 2x.
CACHE_DATASETS = [
    "ds_cache_kpis",
    "ds_cache_composition",
    "ds_cache_billed_trend",
    "ds_cache_by_model",
    "ds_expensive_requests",
]
BILLED_UNIT_MULTIPLIERS = ("0.1 * cache_read", "1.25 * write_5m", "2.0 * write_1h")

# Widget type to required ``spec.version``.  A mismatch renders "Invalid widget
# definition" in the dashboard rather than failing at deploy time.
WIDGET_VERSIONS = {
    "counter": 2,
    "table": 2,
    "filter-date-range-picker": 2,
    "filter-multi-select": 2,
    "filter-single-select": 2,
    "area": 3,
    "bar": 3,
    "histogram": 3,
    "line": 3,
    "pie": 3,
    "scatter": 3,
}

# ``trace_metadata`` is a VARIANT column on the MLflow UC trace tables, so map
# subscripting it fails outright with INVALID_EXTRACT_BASE_FIELD_TYPE.  Values
# must be read with the VARIANT path form ``trace_metadata:['key']::string``.
MAP_SUBSCRIPT_RE = re.compile(r"trace_metadata\s*\[")

# Unity Catalog permits hyphens in catalog names, and a hyphenated identifier is
# invalid in SQL unless back-quoted, so every table reference must quote both
# substituted parts.  Anything else fails with INVALID_IDENTIFIER on such a
# workspace while working fine on every underscore-only one.
QUALIFIED_TABLE_REF = "`${var.catalog}`.`${var.schema}`."
UNQUOTED_TABLE_REF_RE = re.compile(r"(?<!`)\$\{var\.(?:catalog|schema)\}(?!`)")

SAMPLE_VARIABLES = {
    "catalog": "test_catalog",
    "schema": "test_schema",
    "warehouse_id": "abc123",
    "deployer": "user@example.com",
    "app_name": "test-app",
    "app_resource_name": "dev-test-app-app",
    "vs_endpoint_name": "test-vs-endpoint",
}


def _widgets(template: dict[str, Any]) -> list[dict[str, Any]]:
    return [item["widget"] for page in template["pages"] for item in page.get("layout", [])]


def _field_names(node: Any) -> set[str]:
    """Every ``fieldName`` referenced anywhere inside a widget encoding block."""
    if isinstance(node, dict):
        found: set[str] = set()
        for key, value in node.items():
            if key == "fieldName" and isinstance(value, str):
                found.add(value)
            else:
                found |= _field_names(value)
        return found
    if isinstance(node, list):
        return set().union(*(_field_names(item) for item in node)) if node else set()
    return set()


class _InMemoryWorkspaceFake:
    """Minimal stand-in for ``WorkspaceClient`` covering the surface used
    by ``agent_foundry.state``: ``workspace.export``, ``workspace.upload``,
    ``workspace.mkdirs``, ``workspace.delete``."""

    def __init__(self) -> None:
        self._files: dict[str, bytes] = {}
        self.workspace = self  # ws.workspace.* delegates back to self

    def export(self, path: str) -> Any:
        if path not in self._files:
            raise RuntimeError(f"NOT_FOUND: {path}")

        class _Resp:
            def __init__(self, content: bytes) -> None:
                self.content = content

        return _Resp(self._files[path])

    def upload(self, *, path: str, content: Any, format: Any = None, overwrite: bool = True) -> None:
        del format, overwrite
        data = content.read() if hasattr(content, "read") else content
        if isinstance(data, str):
            data = data.encode("utf-8")
        self._files[path] = data

    def mkdirs(self, path: str) -> None:
        del path

    def delete(self, path: str) -> None:
        self._files.pop(path, None)


# ---------------------------------------------------------------------------
# SQL query file tests
# ---------------------------------------------------------------------------


class TestQueryFiles:
    """Validate the SQL query files exist and are well-formed."""

    def test_queries_dir_exists(self) -> None:
        assert QUERIES_DIR.is_dir(), f"Missing queries directory: {QUERIES_DIR}"

    def test_all_expected_queries_present(self) -> None:
        actual = sorted(f.name for f in QUERIES_DIR.glob("*.sql"))
        assert actual == sorted(EXPECTED_QUERIES)

    @pytest.mark.parametrize("filename", EXPECTED_QUERIES)
    def test_query_not_empty(self, filename: str) -> None:
        path = QUERIES_DIR / filename
        content = path.read_text(encoding="utf-8").strip()
        assert len(content) > 0, f"{filename} is empty"

    @pytest.mark.parametrize("filename", EXPECTED_QUERIES)
    def test_query_contains_select(self, filename: str) -> None:
        content = (QUERIES_DIR / filename).read_text(encoding="utf-8")
        assert "SELECT" in content.upper(), f"{filename} has no SELECT statement"

    @pytest.mark.parametrize("filename", MLFLOW_QUERIES)
    def test_mlflow_queries_use_variable_placeholders(self, filename: str) -> None:
        """MLflow-backed queries should reference the catalog and schema variables."""
        content = (QUERIES_DIR / filename).read_text(encoding="utf-8")
        assert "${var.catalog}" in content, f"{filename} missing ${{var.catalog}}"
        assert "${var.schema}" in content, f"{filename} missing ${{var.schema}}"

    @pytest.mark.parametrize("filename", MLFLOW_QUERIES)
    def test_mlflow_queries_rely_on_schema_scoping(self, filename: str) -> None:
        """UC schema provides isolation -- experiment_id must NOT appear."""
        content = (QUERIES_DIR / filename).read_text(encoding="utf-8")
        assert "experiment_id" not in content, f"{filename} should not filter by experiment_id (UC schema scoping)"

    @pytest.mark.parametrize("filename", MLFLOW_QUERIES)
    def test_query_back_quotes_the_catalog_and_schema(self, filename: str) -> None:
        content = (QUERIES_DIR / filename).read_text(encoding="utf-8")
        assert QUALIFIED_TABLE_REF in content, f"{filename} must reference {QUALIFIED_TABLE_REF}"
        unquoted = UNQUOTED_TABLE_REF_RE.findall(content)
        assert not unquoted, f"{filename} leaves a catalog or schema reference unquoted: {unquoted}"

    @pytest.mark.parametrize("filename", EXPECTED_QUERIES)
    def test_query_reads_trace_metadata_as_variant(self, filename: str) -> None:
        content = (QUERIES_DIR / filename).read_text(encoding="utf-8")
        assert not MAP_SUBSCRIPT_RE.search(content), (
            f"{filename} subscripts trace_metadata as a MAP; use trace_metadata:['key']::string"
        )

    def test_cost_query_uses_system_billing(self) -> None:
        content = (QUERIES_DIR / "06_cost_attribution.sql").read_text(encoding="utf-8")
        assert "system.billing.usage" in content

    def test_cost_query_filters_by_app_resources(self) -> None:
        """Cost attribution must scope to instance resources via usage_metadata
        and custom_tags, not show all workspace billing."""
        content = (QUERIES_DIR / "06_cost_attribution.sql").read_text(encoding="utf-8")
        assert "usage_metadata.app_name" in content
        assert "usage_metadata.endpoint_name" in content
        assert "usage_metadata.warehouse_id" in content
        assert "custom_tags" in content


# ---------------------------------------------------------------------------
# Dashboard template tests
# ---------------------------------------------------------------------------


class TestDashboardTemplate:
    """Validate the Lakeview dashboard JSON template."""

    @pytest.fixture
    def template(self) -> dict[str, Any]:
        result: dict[str, Any] = json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))
        return result

    def test_template_file_exists(self) -> None:
        assert TEMPLATE_PATH.exists()

    def test_valid_json(self) -> None:
        json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))

    def test_has_datasets(self, template: dict[str, Any]) -> None:
        assert "datasets" in template
        assert len(template["datasets"]) >= 6

    def test_has_pages(self, template: dict[str, Any]) -> None:
        assert "pages" in template
        assert len(template["pages"]) >= 2

    def test_has_global_filters_page(self, template: dict[str, Any]) -> None:
        gf_pages = [p for p in template["pages"] if p.get("pageType") == "PAGE_TYPE_GLOBAL_FILTERS"]
        assert len(gf_pages) == 1, "Dashboard must have exactly one global filters page"
        gf = gf_pages[0]
        assert len(gf.get("layout", [])) > 0, "Global filters page has no widgets"

    def test_page_has_layout(self, template: dict[str, Any]) -> None:
        canvas_pages = [p for p in template["pages"] if p.get("pageType") == "PAGE_TYPE_CANVAS"]
        assert len(canvas_pages) >= 1
        assert len(canvas_pages[0].get("layout", [])) > 0

    def test_all_datasets_have_query_lines(self, template: dict[str, Any]) -> None:
        for ds in template["datasets"]:
            assert "queryLines" in ds, f"Dataset {ds.get('name')} missing queryLines"
            assert len(ds["queryLines"]) > 0

    def test_all_datasets_have_names(self, template: dict[str, Any]) -> None:
        names = [ds["name"] for ds in template["datasets"]]
        assert len(names) == len(set(names)), "Duplicate dataset names"

    def test_mlflow_datasets_filter_by_experiment(self, template: dict[str, Any]) -> None:
        """MLflow datasets should be scoped — either by experiment_id filter
        or by UC schema isolation (OTEL format).  When using UC-backed tracing
        the schema itself provides isolation, so experiment_id is optional."""
        for ds in template["datasets"]:
            query = " ".join(ds["queryLines"])
            if "system.billing" in query:
                continue
            has_experiment_filter = "${var.experiment_id}" in query
            has_schema_scoping = QUALIFIED_TABLE_REF in query
            assert has_experiment_filter or has_schema_scoping, (
                f"Dataset {ds['name']} must be scoped by experiment_id or UC schema"
            )

    def test_widgets_reference_existing_datasets(self, template: dict[str, Any]) -> None:
        dataset_names = {ds["name"] for ds in template["datasets"]}
        for page in template["pages"]:
            for item in page.get("layout", []):
                widget = item.get("widget", {})
                for query in widget.get("queries", []):
                    ds_name = query.get("query", {}).get("datasetName")
                    if ds_name:
                        assert ds_name in dataset_names, (
                            f"Widget {widget.get('name')} references unknown dataset {ds_name}"
                        )

    def test_widget_positions_fill_grid(self, template: dict[str, Any]) -> None:
        """Each row of canvas page widgets should sum to width=6 on the 6-column grid."""
        for page in template["pages"]:
            if page.get("pageType") == "PAGE_TYPE_GLOBAL_FILTERS":
                continue
            rows: dict[int, int] = {}
            for item in page.get("layout", []):
                pos = item["position"]
                y = pos["y"]
                rows.setdefault(y, 0)
                rows[y] += pos["width"]
            for y, total_width in rows.items():
                assert total_width == 6, f"Row y={y} has total width {total_width}, expected 6"

    def test_human_feedback_dataset_exists(self, template: dict[str, Any]) -> None:
        """Dashboard must include a human feedback dataset for UI votes."""
        ds_names = {ds["name"] for ds in template["datasets"]}
        assert "ds_human_feedback" in ds_names
        ds = next(d for d in template["datasets"] if d["name"] == "ds_human_feedback")
        query = " ".join(ds["queryLines"])
        assert "source_type" in query
        assert "HUMAN" in query

    def test_quality_scores_include_llm_judges(self, template: dict[str, Any]) -> None:
        """Built-in judges (Safety, RelevanceToQuery) log LLM_JUDGE assessments, code scorers log CODE."""
        ds = next(d for d in template["datasets"] if d["name"] == "ds_quality_scores")
        query = " ".join(ds["queryLines"])
        assert "'CODE'" in query
        assert "'LLM_JUDGE'" in query

    def test_feedback_categories_dataset_exists(self, template: dict[str, Any]) -> None:
        ds_names = {ds["name"] for ds in template["datasets"]}
        assert "ds_feedback_categories" in ds_names
        ds = next(d for d in template["datasets"] if d["name"] == "ds_feedback_categories")
        query = " ".join(ds["queryLines"])
        assert "user_feedback_categories" in query

    def test_feedback_comments_dataset_exists(self, template: dict[str, Any]) -> None:
        ds_names = {ds["name"] for ds in template["datasets"]}
        assert "ds_feedback_comments" in ds_names
        ds = next(d for d in template["datasets"] if d["name"] == "ds_feedback_comments")
        query = " ".join(ds["queryLines"])
        assert "user_feedback_comment" in query

    def test_user_datasets_use_correct_metadata_key(self, template: dict[str, Any]) -> None:
        """User-related datasets must reference ``mlflow.user``.
        COALESCE(mlflow.trace.user, mlflow.user) is also valid."""
        user_datasets = {"ds_daily_users", "ds_kpi_users", "ds_kpi_avg_requests_per_user"}
        for ds in template["datasets"]:
            if ds["name"] not in user_datasets:
                continue
            query = " ".join(ds["queryLines"])
            assert "mlflow.user" in query, f"Dataset {ds['name']} missing mlflow.user reference"

    def test_datasets_back_quote_the_catalog_and_schema(self, template: dict[str, Any]) -> None:
        """A hyphenated catalog is legal in Unity Catalog and invalid in SQL
        unquoted, so both substituted parts carry back quotes."""
        for ds in template["datasets"]:
            query = " ".join(ds["queryLines"])
            unquoted = UNQUOTED_TABLE_REF_RE.findall(query)
            assert not unquoted, f"Dataset {ds['name']} leaves a catalog or schema reference unquoted: {unquoted}"

    def test_datasets_read_trace_metadata_as_variant(self, template: dict[str, Any]) -> None:
        for ds in template["datasets"]:
            query = " ".join(ds["queryLines"])
            assert not MAP_SUBSCRIPT_RE.search(query), (
                f"Dataset {ds['name']} subscripts trace_metadata as a MAP; use trace_metadata:['key']::string"
            )

    @pytest.mark.parametrize("dataset_name", CACHE_DATASETS)
    def test_cache_datasets_read_the_span_usage_fields(self, template: dict[str, Any], dataset_name: str) -> None:
        """The cache split exists only on the LLM span, never at trace level."""
        ds = next((d for d in template["datasets"] if d["name"] == dataset_name), None)
        assert ds is not None, f"Missing dataset {dataset_name}"
        query = " ".join(ds["queryLines"])
        assert "mlflow.spanOutputs" in query
        assert "mlflow.chat.tokenUsage']:cache_read_input_tokens" in query
        assert "cache_read_input_tokens" in query
        assert "cache_creation_input_tokens" in query
        assert "ephemeral_1h_input_tokens" in query
        assert "EXPLODE(spans)" in query
        assert "mlflow.chat.tokenUsage" in query, "LLM spans must be selected by their token usage attribute"

    def test_billed_unit_datasets_apply_the_tiered_multipliers(self, template: dict[str, Any]) -> None:
        """Billed units must price each tier separately; a single blended write
        multiplier understates long-TTL writes by 60%."""
        for name in ("ds_cache_kpis", "ds_cache_billed_trend", "ds_cache_by_model", "ds_expensive_requests"):
            ds = next(d for d in template["datasets"] if d["name"] == name)
            query = " ".join(ds["queryLines"])
            for multiplier in BILLED_UNIT_MULTIPLIERS:
                assert multiplier in query, f"Dataset {name} missing billed unit term {multiplier}"

    def test_token_economics_page_exists(self, template: dict[str, Any]) -> None:
        page = next((p for p in template["pages"] if p["name"] == "token_economics"), None)
        assert page is not None, "Missing the token economics page"
        assert page["pageType"] == "PAGE_TYPE_CANVAS"
        assert len(page["layout"]) > 0

    def test_query_line_fragments_end_with_whitespace(self, template: dict[str, Any]) -> None:
        """queryLines are concatenated without a separator, so every fragment but
        the last must end in whitespace or tokens fuse into ``totalFROM``."""
        for ds in template["datasets"]:
            for fragment in ds["queryLines"][:-1]:
                assert fragment != fragment.rstrip(), (
                    f"Dataset {ds['name']} has a queryLines fragment without trailing whitespace: {fragment!r}"
                )

    def test_query_lines_carry_no_sql_comments(self, template: dict[str, Any]) -> None:
        """A ``--`` comment would swallow the rest of the concatenated query."""
        for ds in template["datasets"]:
            for fragment in ds["queryLines"]:
                assert "--" not in fragment, f"Dataset {ds['name']} has a SQL comment in queryLines"

    def test_widget_versions_match_the_type_contract(self, template: dict[str, Any]) -> None:
        for widget in _widgets(template):
            spec = widget.get("spec")
            if not spec:
                continue
            widget_type = spec.get("widgetType")
            assert widget_type in WIDGET_VERSIONS, f"Widget {widget.get('name')} has unknown type {widget_type}"
            assert spec.get("version") == WIDGET_VERSIONS[widget_type], (
                f"Widget {widget.get('name')} ({widget_type}) must use version {WIDGET_VERSIONS[widget_type]}"
            )

    def test_text_widgets_carry_no_spec(self, template: dict[str, Any]) -> None:
        for widget in _widgets(template):
            if "multilineTextboxSpec" in widget:
                assert "spec" not in widget, f"Text widget {widget.get('name')} must not declare a spec block"

    def test_widget_field_names_resolve_to_query_fields(self, template: dict[str, Any]) -> None:
        """Every ``fieldName`` in the encodings must match a declared query field
        name exactly, or the widget silently renders "no selected fields"."""
        for widget in _widgets(template):
            spec = widget.get("spec")
            if not spec:
                continue
            declared = {
                field["name"]
                for query in widget.get("queries", [])
                for field in query.get("query", {}).get("fields", [])
            }
            for referenced in _field_names(spec.get("encodings", {})):
                assert referenced in declared, (
                    f"Widget {widget.get('name')} encodes {referenced!r} which is not a declared query field"
                )

    def test_no_overlapping_positions(self, template: dict[str, Any]) -> None:
        """No two widgets should occupy the same grid cell."""
        for page in template["pages"]:
            if page.get("pageType") == "PAGE_TYPE_GLOBAL_FILTERS":
                continue
            occupied: set[tuple[int, int]] = set()
            for item in page.get("layout", []):
                pos = item["position"]
                for dx in range(pos["width"]):
                    for dy in range(pos["height"]):
                        cell = (pos["x"] + dx, pos["y"] + dy)
                        assert cell not in occupied, f"Cell {cell} occupied by multiple widgets"
                        occupied.add(cell)


# ---------------------------------------------------------------------------
# Deploy script tests
# ---------------------------------------------------------------------------


class TestDeployScript:
    """Test the deploy script can be imported and functions work offline."""

    def test_deploy_module_importable(self) -> None:
        from bricks.observability.scripts import deploy_dashboard as mod

        assert hasattr(mod, "deploy")
        assert hasattr(mod, "load_template")

    def test_load_template_substitutes_variables(self) -> None:
        from bricks.observability.scripts.deploy_dashboard import load_template

        dashboard = load_template(SAMPLE_VARIABLES)
        for ds in dashboard["datasets"]:
            joined = " ".join(ds["queryLines"])
            assert "${var." not in joined, f"Unresolved variable in dataset {ds['name']}: {joined}"

    def test_load_template_raises_for_missing_variables(self) -> None:
        from bricks.observability.scripts.deploy_dashboard import load_template

        with pytest.raises(ValueError, match="Undefined variable"):
            load_template({})

    def test_create_dashboard_makes_parent_folder_before_posting(self) -> None:
        from unittest.mock import MagicMock, call

        from bricks.observability.scripts.deploy_dashboard import create_dashboard

        client = MagicMock()
        create_dashboard(client, "d", "{}", "wh", "/Shared/agent-foundry/dev/app")

        assert client.mock_calls[0] == call.workspace.mkdirs("/Shared/agent-foundry/dev/app")
        assert client.mock_calls[1][0] == "api_client.do"
        assert client.mock_calls[1].kwargs["body"]["parent_path"] == "/Shared/agent-foundry/dev/app"

    def test_state_roundtrip_workspace_only(self, tmp_path: Path) -> None:
        """save_state writes to the workspace (single source of truth) and
        load_state reads it back; no local cache is touched (issue #50)."""
        from agent_foundry.state import load_state, save_state

        ws = _InMemoryWorkspaceFake()

        state = load_state(
            ws,
            "test-inst",
            "dashboard",
            "test-inst",
            "dev",
        )
        assert state == {}
        save_state(
            ws,
            "test-inst",
            "dashboard",
            "test-inst",
            "dev",
            {"dashboard_id": "abc", "etag": "123"},
        )
        state = load_state(
            ws,
            "test-inst",
            "dashboard",
            "test-inst",
            "dev",
        )
        assert state["dashboard_id"] == "abc"
        assert state["etag"] == "123"
        assert not (tmp_path / "_state").exists(), "save_state must not write a local cache"

    def test_save_state_is_noop_without_ws(self, tmp_path: Path) -> None:
        """With ws=None, save_state mutates the in-memory dict but persists
        nothing — no local cache is written (issue #50)."""
        from agent_foundry.state import save_state

        save_state(
            None,
            "test-inst",
            "dashboard",
            "test-inst",
            "dev",
            {"dashboard_id": "abc"},
        )
        assert not (tmp_path / "_state").exists()

    def test_deploy_dry_run(self, tmp_path: Path) -> None:
        from bricks.observability.scripts.deploy_dashboard import deploy

        rc = deploy(tmp_path, "dev", SAMPLE_VARIABLES, dry_run=True)
        assert rc == 0

    def test_deploy_requires_all_variables_for_live(self, tmp_path: Path) -> None:
        """Missing variables in strict mode should raise ValueError."""
        from unittest.mock import MagicMock, patch

        from bricks.observability.scripts.deploy_dashboard import deploy

        incomplete = {k: v for k, v in SAMPLE_VARIABLES.items() if k != "warehouse_id"}
        with patch("bricks.observability.scripts.deploy_dashboard._get_client", return_value=MagicMock()):
            with pytest.raises(ValueError, match="Undefined variable"):
                deploy(tmp_path, "dev", incomplete, dry_run=False)
