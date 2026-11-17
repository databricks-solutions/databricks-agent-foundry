"""Tests for ``payloads.agent.runtime.report_tool``."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from payloads.agent.runtime.chart_tool import ChartSource
from payloads.agent.runtime.mcp_wrapper import FilteredMCPServerStreamableHttp
from payloads.agent.runtime.report_tool import (
    MAX_FILENAME_CHARS,
    MAX_REPORT_CHARS,
    REPORT_TOOL_NAME,
    ReportTool,
    place_charts,
    prepare_report_tool,
    render_report,
    report_filename,
)
from payloads.agent.runtime.request_context import RequestState

_CALL_ID = "call-1"
_SQL_URL = "https://example.invalid/api/2.0/mcp/sql"
_SQL_TOOL = "sql__execute_sql_read_only"
_QUERY = "SELECT product, dbus FROM usage"
_CHART = {"id": "dbus", "query": _QUERY, "type": "bar", "x": "product", "y": ["dbus"], "title": "DBUs"}


def _server() -> FilteredMCPServerStreamableHttp:
    return FilteredMCPServerStreamableHttp(
        params={"url": _SQL_URL},
        name_prefix="sql__",
        server_url=_SQL_URL,
        cache_tools_list=False,
        name="sql",
    )


def _envelope(columns: list[str], rows: list[list[Any]]) -> SimpleNamespace:
    body = {
        "status": {"state": "SUCCEEDED"},
        "manifest": {"schema": {"columns": [{"name": name} for name in columns]}, "total_row_count": len(rows)},
        "result": {"data_array": rows},
    }
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(body))], isError=False)


def _chart_source(response: Any) -> ChartSource:
    server = _server()
    server.call_tool_unbounded = AsyncMock(return_value=response)  # type: ignore[method-assign]
    return ChartSource(server, _SQL_TOOL)


def _ctx() -> SimpleNamespace:
    return SimpleNamespace(context=RequestState(), tool_call_id=_CALL_ID)


async def _report(ctx: Any = None, source: ChartSource | None = None, **args: Any) -> tuple[str, dict[str, str]]:
    ctx = ctx if ctx is not None else _ctx()
    out = await ReportTool(source)(ctx, json.dumps(args))
    return out, getattr(ctx.context, "ui_parts", {})


class TestPrepareReportTool:
    def test_offered_without_charts_when_no_sql_tool_is_mounted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ENABLE_REPORTS", "true")
        monkeypatch.setenv("ENABLE_CHARTS", "true")
        report_tool = prepare_report_tool([])
        assert report_tool is not None
        assert [tool.name for tool in report_tool.tools] == [REPORT_TOOL_NAME]
        assert "charts" not in report_tool.tools[0].params_json_schema["properties"]
        assert "{{chart:<id>}}" not in report_tool.guidance

    def test_offers_charts_when_charts_are_on_and_sql_is_mounted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ENABLE_REPORTS", "true")
        monkeypatch.setenv("ENABLE_CHARTS", "true")
        report_tool = prepare_report_tool([_server()])
        assert report_tool is not None
        charts = report_tool.tools[0].params_json_schema["properties"]["charts"]
        assert charts["items"]["required"][0] == "id"
        assert "{{chart:<id>}}" in report_tool.guidance

    def test_no_charts_when_charts_are_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ENABLE_REPORTS", "true")
        monkeypatch.delenv("ENABLE_CHARTS", raising=False)
        report_tool = prepare_report_tool([_server()])
        assert report_tool is not None
        assert "charts" not in report_tool.tools[0].params_json_schema["properties"]

    def test_not_offered_when_reports_are_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ENABLE_REPORTS", raising=False)
        assert prepare_report_tool([_server()]) is None


class TestReportFilename:
    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            ("Q3 Cost Review", "q3-cost-review"),
            ("  Über / ../etc/passwd  ", "ber-etc-passwd"),
            ("---", "report"),
            ("", "report"),
        ],
    )
    def test_keeps_lower_case_letters_digits_and_single_hyphens(self, title: str, expected: str) -> None:
        assert report_filename(title) == expected

    def test_is_cut_without_a_trailing_hyphen(self) -> None:
        stem = report_filename("a" * (MAX_FILENAME_CHARS - 1) + " b")
        assert len(stem) <= MAX_FILENAME_CHARS
        assert not stem.endswith("-")


class TestRenderReport:
    def test_wraps_the_body_in_a_four_backtick_fence_with_the_filename(self) -> None:
        fence = render_report("Weekly", "# Weekly\n\nAll good.\n")
        assert fence == "````markdown-report:weekly\n# Weekly\n\nAll good.\n````"

    def test_fence_is_longer_than_any_backtick_run_in_the_body(self) -> None:
        body = "# Code\n\n`````\nnested\n`````\n\n```sql\nSELECT 1\n```"
        fence = render_report("Code", body)
        assert fence.startswith("``````markdown-report:code\n")
        assert fence.endswith("\n``````")


@pytest.mark.asyncio
class TestReportTool:
    async def test_files_the_fence_and_returns_only_a_summary(self) -> None:
        out, ui_parts = await _report(title="Q3 Review", markdown="# Q3 Review\n\nRevenue grew.")
        assert ui_parts[_CALL_ID] == "````markdown-report:q3-review\n# Q3 Review\n\nRevenue grew.\n````"
        assert out == 'Report shown to the user: "Q3 Review", 26 characters.'
        assert "Revenue grew" not in out

    async def test_rejects_a_report_over_the_limit(self) -> None:
        out, ui_parts = await _report(title="Big", markdown="x" * (MAX_REPORT_CHARS + 1))
        assert out.startswith("Error: `markdown`")
        assert ui_parts == {}

    async def test_rejects_missing_and_extra_arguments(self) -> None:
        out, _ = await _report(markdown="# Hi", format="pdf")
        assert "`title`" in out and "`format`" in out

    async def test_without_a_ui_outbox_reports_the_error(self) -> None:
        out, _ = await _report(ctx=SimpleNamespace(context=None, tool_call_id=_CALL_ID), title="T", markdown="# T")
        assert out == "Error: reports can't be shown in this conversation."


def test_place_charts_puts_each_fence_in_its_own_block() -> None:
    assert place_charts("Intro\n{{chart:a}}\nEnd", {"a": "```echarts-chart\n{}\n```"}) == (
        "Intro\n\n\n```echarts-chart\n{}\n```\n\n\nEnd"
    )


@pytest.mark.asyncio
class TestReportCharts:
    _MARKDOWN = "# Usage\n\n{{chart:dbus}}\n\nVector Search leads."

    async def test_places_the_chart_and_returns_its_summary_not_the_rows(self) -> None:
        source = _chart_source(_envelope(["product", "dbus"], [["VECTOR_SEARCH", "16.04"], ["APPS", "6.38"]]))
        out, ui_parts = await _report(source=source, title="Usage", markdown=self._MARKDOWN, charts=[_CHART])
        report = ui_parts[_CALL_ID]
        assert report.startswith("````markdown-report:usage\n# Usage\n\n")
        chart_body = report.split("```echarts-chart\n", 1)[1].split("\n```", 1)[0]
        assert json.loads(chart_body)["data"] == [
            {"product": "VECTOR_SEARCH", "dbus": 16.04},
            {"product": "APPS", "dbus": 6.38},
        ]
        assert "{{chart:" not in report
        assert out.startswith('Report shown to the user: "Usage", 45 characters, 1 chart.\n`dbus`: Chart shown')
        assert "echarts-chart" not in out

    async def test_a_failing_chart_names_it_and_shows_no_report(self) -> None:
        source = _chart_source(_envelope(["product", "dbus"], [["APPS", "6.38"]]))
        bad = {**_CHART, "y": ["cost"]}
        out, ui_parts = await _report(source=source, title="Usage", markdown=self._MARKDOWN, charts=[bad])
        assert out.startswith("Chart `dbus`: Error: no column cost")
        assert ui_parts == {}

    @pytest.mark.parametrize(
        ("markdown", "charts", "message"),
        [
            ("# U\n{{chart:dbus}}", [], "no chart in `charts` for dbus"),
            ("# U", [_CHART], "place dbus in the markdown"),
            ("# U\n{{chart:dbus}}\n{{chart:dbus}}", [_CHART], "place each chart once"),
            ("# U\n{{chart:dbus}}", [_CHART, _CHART], "chart ids must be unique"),
        ],
    )
    async def test_charts_and_placeholders_must_match(
        self, markdown: str, charts: list[dict[str, Any]], message: str
    ) -> None:
        out, ui_parts = await _report(source=_chart_source(None), title="U", markdown=markdown, charts=charts)
        assert message in out
        assert ui_parts == {}

    async def test_charts_are_refused_without_a_chart_source(self) -> None:
        out, ui_parts = await _report(title="Usage", markdown=self._MARKDOWN, charts=[_CHART])
        assert out.startswith("Error: charts can't be added to reports here")
        assert ui_parts == {}
