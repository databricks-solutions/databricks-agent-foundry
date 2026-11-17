"""Tests for ``payloads.agent.runtime.chart_tool``."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from payloads.agent.runtime.chart_helpers import MAX_DATA_ROWS
from payloads.agent.runtime.chart_tool import CHART_TOOL_NAME, MAX_SERIES, ChartSource, ChartTool, prepare_chart_tool
from payloads.agent.runtime.mcp_wrapper import FilteredMCPServerStreamableHttp
from payloads.agent.runtime.request_context import RequestState

_SQL_URL = "https://example.invalid/api/2.0/mcp/sql"
_QUERY = "SELECT month, region, revenue FROM shared_catalog.sales.monthly"
_CALL_ID = "call-1"


def _server(url: str = _SQL_URL) -> FilteredMCPServerStreamableHttp:
    return FilteredMCPServerStreamableHttp(
        params={"url": url},
        name_prefix="sql__",
        server_url=url,
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


def _ctx() -> SimpleNamespace:
    return SimpleNamespace(context=RequestState(), tool_call_id=_CALL_ID)


async def _chart(response: Any, ctx: Any = None, **args: Any) -> tuple[str, dict[str, str]]:
    server = _server()
    server.call_tool_unbounded = AsyncMock(return_value=response)  # type: ignore[method-assign]
    ctx = ctx if ctx is not None else _ctx()
    out = await ChartTool(ChartSource(server, "sql__execute_sql_read_only"))(ctx, json.dumps({"query": _QUERY, **args}))
    return out, getattr(ctx.context, "ui_parts", {})


def _spec(ui_parts: dict[str, str]) -> dict[str, Any]:
    fence = ui_parts[_CALL_ID]
    assert fence.startswith("```echarts-chart\n") and fence.endswith("\n```")
    spec: dict[str, Any] = json.loads(fence.removeprefix("```echarts-chart\n").removesuffix("\n```"))
    return spec


_WIDE = _envelope(["month", "revenue"], [["2026-01", "10.50"], ["2026-02", "12"], ["2026-03", None]])
_LONG = _envelope(
    ["month", "region", "revenue"],
    [["2026-01", "EU", 1], ["2026-01", "US", 2], ["2026-02", "EU", 3]],
)


class TestPrepareChartTool:
    def test_offered_when_charts_are_on_and_sql_is_mounted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ENABLE_CHARTS", "true")
        chart_tool = prepare_chart_tool([_server()])
        assert chart_tool is not None
        assert [tool.name for tool in chart_tool.tools] == [CHART_TOOL_NAME]
        assert CHART_TOOL_NAME in chart_tool.guidance

    def test_not_offered_when_charts_are_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ENABLE_CHARTS", "false")
        assert prepare_chart_tool([_server()]) is None

    def test_not_offered_without_the_sql_server(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ENABLE_CHARTS", "true")
        assert prepare_chart_tool([_server(url="https://example.invalid/api/2.0/mcp/genie/space")]) is None


@pytest.mark.asyncio
class TestChartTool:
    async def test_files_the_fence_and_returns_only_a_summary(self) -> None:
        out, ui_parts = await _chart(_WIDE, type="line", x="month", y=["revenue"], title="Revenue")
        spec = _spec(ui_parts)
        assert spec["data"] == [
            {"month": "2026-01", "revenue": 10.5},
            {"month": "2026-02", "revenue": 12.0},
            {"month": "2026-03", "revenue": None},
        ]
        assert spec["title"] == "Revenue"
        assert out.splitlines() == [
            'Chart shown to the user: line "Revenue", 3 rows, `month` from 2026-01 to 2026-03.',
            "- revenue: min 10.5 (2026-01), max 12 (2026-02), last 12",
        ]

    async def test_pivots_long_rows_by_series(self) -> None:
        out, ui_parts = await _chart(_LONG, type="bar", x="month", y=["revenue"], series="region")
        spec = _spec(ui_parts)
        assert spec["y"] == ["EU", "US"]
        assert spec["data"] == [
            {"month": "2026-01", "EU": 1.0, "US": 2.0},
            {"month": "2026-02", "EU": 3.0, "US": None},
        ]
        assert "- US: min 2 (2026-01), max 2 (2026-01), last 2" in out

    async def test_scatter_gets_numeric_x_and_only_charted_columns(self) -> None:
        response = _envelope(
            ["zip", "distance", "fare", "trips"],
            [["10001", "2.95", "12.5", 1227], ["10003", "2.21", "10.6", 50]],
        )
        out, ui_parts = await _chart(response, type="scatter", x="distance", y=["fare"], size="trips")
        assert _spec(ui_parts)["data"] == [
            {"distance": 2.95, "fare": 12.5, "trips": 1227.0},
            {"distance": 2.21, "fare": 10.6, "trips": 50.0},
        ]
        assert out.splitlines()[1:] == [
            "- fare: min 10.6 (2.21), max 12.5 (2.95), last 10.6",
            "- trips (bubble size): min 50 (2.21), max 1227 (2.95), last 50",
        ]
        assert "`distance` from 2.21 to 2.95." in out

    async def test_category_x_stays_text(self) -> None:
        response = _envelope(["hour", "trips"], [["8", 3], ["18", 5]])
        _, ui_parts = await _chart(response, type="line", x="hour", y=["trips"])
        assert [row["hour"] for row in _spec(ui_parts)["data"]] == ["8", "18"]

    async def test_rejects_too_many_series(self) -> None:
        rows = [["2026-01", f"r{i}", i] for i in range(MAX_SERIES + 1)]
        response = _envelope(["month", "region", "revenue"], rows)
        out, ui_parts = await _chart(response, type="bar", x="month", y=["revenue"], series="region")
        assert out.startswith(f"Error: `region` has more than {MAX_SERIES} values")
        assert ui_parts == {}

    async def test_rejects_repeated_x_and_series_pairs(self) -> None:
        response = _envelope(["month", "region", "revenue"], [["2026-01", "EU", 1], ["2026-01", "EU", 2]])
        out, _ = await _chart(response, type="bar", x="month", y=["revenue"], series="region")
        assert "aggregate them with GROUP BY" in out

    async def test_rejects_more_rows_than_a_chart_holds(self) -> None:
        rows = [[i, i] for i in range(MAX_DATA_ROWS + 1)]
        out, ui_parts = await _chart(_envelope(["n", "v"], rows), type="scatter", x="n", y=["v"])
        assert out == f"Error: the chart would have {MAX_DATA_ROWS + 1} rows; aggregate in SQL to at most 500."
        assert ui_parts == {}

    async def test_names_missing_columns(self) -> None:
        out, _ = await _chart(_WIDE, type="line", x="month", y=["profit"])
        assert out == "Error: no column profit; the result has month, revenue."

    async def test_series_needs_a_single_y(self) -> None:
        out, _ = await _chart(_LONG, type="bar", x="month", y=["revenue", "region"], series="region")
        assert out.startswith("Error: series needs exactly one y, no y2 or size, and a type other than sankey.")

    async def test_sankey_sums_flows_and_summarises_the_largest(self) -> None:
        response = _envelope(
            ["src", "dst", "cost"],
            [
                ["Budget", "Compute", "60"],
                ["Budget", "Storage", 30],
                ["Compute", "Jobs", 40],
                ["Budget", "Compute", 5],
                ["Budget", "Unused", 0],
                [None, "Jobs", 3],
            ],
        )
        out, ui_parts = await _chart(response, type="sankey", x="src", target="dst", y=["cost"], title="Spend")
        spec = _spec(ui_parts)
        assert spec["target"] == "dst"
        assert spec["data"] == [
            {"src": "Budget", "dst": "Compute", "cost": 65.0},
            {"src": "Budget", "dst": "Storage", "cost": 30.0},
            {"src": "Compute", "dst": "Jobs", "cost": 40.0},
        ]
        assert out.splitlines() == [
            'Chart shown to the user: sankey "Spend", 4 nodes, 3 flows, 95 leaving the start nodes.',
            "- largest flows: Budget → Compute 65; Compute → Jobs 40; Budget → Storage 30",
        ]

    async def test_sankey_rejects_negative_amounts(self) -> None:
        response = _envelope(["src", "dst", "cost"], [["A", "B", -1]])
        out, ui_parts = await _chart(response, type="sankey", x="src", target="dst", y=["cost"])
        assert out == "Error: `cost` has negative amounts; a sankey needs flows above 0."
        assert ui_parts == {}

    async def test_sankey_loops_come_back_with_the_nodes(self) -> None:
        response = _envelope(["src", "dst", "cost"], [["A", "B", 1], ["B", "A", 1]])
        out, ui_parts = await _chart(response, type="sankey", x="src", target="dst", y=["cost"])
        assert out.startswith("Error: sankey flows must not loop back (A, B)")
        assert ui_parts == {}

    async def test_sankey_needs_a_target(self) -> None:
        out, _ = await _chart(_WIDE, type="sankey", x="month", y=["revenue"])
        assert out == "Error: sankey requires target"

    async def test_layout_conflicts_come_back_as_errors(self) -> None:
        out, ui_parts = await _chart(_WIDE, type="line", x="month", y=["revenue"], horizontal=True)
        assert out.startswith("Error: ")
        assert ui_parts == {}

    async def test_needs_a_run_that_can_show_charts(self) -> None:
        out, _ = await _chart(
            _WIDE, ctx=SimpleNamespace(context=None, tool_call_id=_CALL_ID), type="line", x="month", y=["revenue"]
        )
        assert out == "Error: charts can't be shown in this conversation."

    async def test_reports_an_unavailable_sql_tool(self) -> None:
        server = _server()
        server.call_tool_unbounded = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]
        args = json.dumps({"query": _QUERY, "type": "line", "x": "month", "y": ["revenue"]})
        out = await ChartTool(ChartSource(server, "sql__execute_sql_read_only"))(_ctx(), args)
        assert out == "Error: the SQL tool is unavailable right now; continue without it."
