"""Chart a SQL result without passing its rows through the model: ``_chart``.

The model names a query and the columns to plot.  The tool reruns the query
through the managed SQL MCP server's read-only tool, as the user (the
warehouse answers from its result cache), builds the ``echarts-chart`` fence
with :func:`render_chart`, and files it in the request's
:attr:`RequestState.ui_parts` so the output path shows it right after the
tool result.  The model only gets a short summary.  Offered when charts are
enabled in the UI and the read-only SQL tool is mounted.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from operator import itemgetter
from typing import Any, Final, Literal, cast, get_args

from agents import FunctionTool
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from agent_foundry import env
from payloads.agent.runtime.chart_helpers import (
    MAX_AXIS_NAME_LENGTH,
    MAX_DATA_ROWS,
    MAX_REFERENCE_MARKS,
    MAX_SANKEY_NODES,
    ReferenceBand,
    ReferenceLine,
    YAxisOptions,
    render_chart,
)
from payloads.agent.runtime.mcp_wrapper import FilteredMCPServerStreamableHttp
from payloads.agent.runtime.request_context import ui_outbox
from payloads.agent.runtime.sql_rerun import (
    MAX_QUERY_CHARS,
    RerunError,
    find_read_only_sql_tool,
    invalid_args_message,
    rerun_query,
)

CHART_TOOL_NAME: Final = "_chart"
MAX_SERIES: Final = 12
MAX_TITLE_CHARS: Final = 200
_NULL_SERIES: Final = "NULL"
_LARGEST_FLOWS: Final = 3
ChartType = Literal["bar", "line", "area", "pie", "scatter", "sankey"]
_CHART_TYPES: Final[tuple[str, ...]] = get_args(ChartType)

_GUIDANCE: Final = (
    "## Charts\n"
    f"- To chart SQL data, call `{CHART_TOOL_NAME}` with the query and the columns to plot. It runs the query "
    "and shows the chart to the user, so the rows never pass through you. Aggregate in SQL to at most "
    f"{MAX_DATA_ROWS} rows; for long-format results (one row per x and group) pass the group column as `series`.\n"
    '- For flows from one stage to the next (spend by area, user paths), use `type: "sankey"` with `x` = source, '
    f"`target` = destination and one `y`; at most {MAX_SANKEY_NODES} nodes and no loops. Prefix names with their "
    "stage when a name repeats across stages.\n"
    "- Describe the chart from the summary the tool returns. Don't repeat its data or draw it again.\n"
    "- Notes like `[An interactive chart was shown here …]` stand in for earlier charts in this history. "
    "Never write them yourself."
)
_DESCRIPTION: Final = (
    "Run a read-only SQL query as the user and show its result to the user as an interactive chart. You get a "
    "short summary, not the rows. `y` lists numeric columns, one series each; or pass one `y` and a `series` "
    f"column to pivot long-format rows into up to {MAX_SERIES} series. At most {MAX_DATA_ROWS} rows after the pivot. "
    "sankey: `x` = source, `target` = destination, one `y` = amount."
)
_AXIS_PARAMS: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "maxLength": MAX_AXIS_NAME_LENGTH},
        "log": {"type": "boolean"},
        "min": {"type": "number"},
        "max": {"type": "number"},
    },
    "additionalProperties": False,
}
_MARK_VALUE: Final[dict[str, Any]] = {"type": ["number", "string"]}
_MARK_AXIS: Final[dict[str, Any]] = {
    "enum": ["x", "y"],
    "description": "x = category/time axis, y = left value axis.",
}
CHART_PARAMS: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "minLength": 1, "maxLength": MAX_QUERY_CHARS},
        "type": {"enum": list(_CHART_TYPES)},
        "x": {"type": "string", "description": "Column for the x axis (pie: slice names; sankey: flow source)."},
        "target": {"type": "string", "description": "sankey only: column for the flow destination."},
        "y": {"type": "array", "items": {"type": "string"}, "minItems": 1, "description": "Numeric columns."},
        "title": {"type": "string", "maxLength": MAX_TITLE_CHARS},
        "series": {"type": "string", "description": "Column whose values become series; needs exactly one `y`."},
        "x_type": {"enum": ["category", "time"], "description": "time: x holds ISO dates; gaps stay visible."},
        "x_name": {"type": "string", "maxLength": MAX_AXIS_NAME_LENGTH},
        "y_axis": _AXIS_PARAMS,
        "stacked": {"type": "boolean", "description": "bar and area only."},
        "horizontal": {"type": "boolean", "description": "bar only."},
        "y2": {"type": "array", "items": {"type": "string"}, "minItems": 1, "description": "Right-axis columns."},
        "y2_axis": _AXIS_PARAMS,
        "size": {"type": "string", "description": "scatter only: column for bubble size."},
        "reference_lines": {
            "type": "array",
            "maxItems": MAX_REFERENCE_MARKS,
            "items": {
                "type": "object",
                "properties": {"axis": _MARK_AXIS, "value": _MARK_VALUE, "label": {"type": "string"}},
                "required": ["axis", "value"],
                "additionalProperties": False,
            },
        },
        "reference_bands": {
            "type": "array",
            "maxItems": MAX_REFERENCE_MARKS,
            "items": {
                "type": "object",
                "properties": {
                    "axis": _MARK_AXIS,
                    "from": _MARK_VALUE,
                    "to": _MARK_VALUE,
                    "label": {"type": "string"},
                },
                "required": ["axis", "from", "to"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["query", "type", "x", "y"],
    "additionalProperties": False,
}
_USAGE: Final = (
    f"Give `query` (1 to {MAX_QUERY_CHARS:,} characters), `type`, `x` and `y`; `series` needs exactly one `y`, "
    "no `y2` or `size`, and a type other than sankey."
)
_NO_UI: Final = "Error: charts can't be shown in this conversation."


class _AxisArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=MAX_AXIS_NAME_LENGTH)
    log: bool | None = None
    min: float | None = None
    max: float | None = None


class _ReferenceLineArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    axis: Literal["x", "y"]
    value: float | str
    label: str | None = None


class _ReferenceBandArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    axis: Literal["x", "y"]
    from_: float | str = Field(alias="from")
    to: float | str
    label: str | None = None


class ChartArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=MAX_QUERY_CHARS)
    type: ChartType
    x: str = Field(min_length=1)
    target: str | None = Field(default=None, min_length=1)
    y: list[str] = Field(min_length=1)
    title: str | None = Field(default=None, max_length=MAX_TITLE_CHARS)
    series: str | None = Field(default=None, min_length=1)
    x_type: Literal["category", "time"] | None = None
    x_name: str | None = Field(default=None, max_length=MAX_AXIS_NAME_LENGTH)
    y_axis: _AxisArgs | None = None
    stacked: bool = False
    horizontal: bool = False
    y2: list[str] | None = Field(default=None, min_length=1)
    y2_axis: _AxisArgs | None = None
    size: str | None = None
    reference_lines: list[_ReferenceLineArgs] = Field(default_factory=list, max_length=MAX_REFERENCE_MARKS)
    reference_bands: list[_ReferenceBandArgs] = Field(default_factory=list, max_length=MAX_REFERENCE_MARKS)

    @model_validator(mode="after")
    def _series_needs_one_y(self) -> ChartArgs:
        if self.series is not None and (len(self.y) != 1 or self.y2 or self.size or self.type == "sankey"):
            raise ValueError("series needs exactly one y, no y2 or size, and a type other than sankey")
        return self

    def numeric_columns(self) -> list[str]:
        return [*self.y, *(self.y2 or []), *([self.size] if self.size else [])]

    def query_columns(self) -> list[str]:
        extra = [name for name in (self.target, self.series) if name]
        return [self.x, *self.numeric_columns(), *extra]


class _ChartError(Exception):
    """A message for the model about a result that can't be charted."""


def _to_number(value: Any) -> float | None:
    """A finite number from a SQL cell (decimals arrive as strings), else ``None``."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _all_numeric(rows: list[list[Any]], index: int) -> bool:
    return all(_to_number(row[index]) is not None for row in rows)


def _records(result: dict[str, Any], args: ChartArgs) -> list[dict[str, Any]]:
    """The rows as records holding only the charted columns, numeric ones as numbers."""
    columns: list[str] = result["columns"]
    used = list(dict.fromkeys(args.query_columns()))
    missing = [name for name in used if name not in columns]
    if missing:
        raise _ChartError(f"Error: no column {', '.join(missing)}; the result has {', '.join(columns)}.")
    if result.get("total_rows_is_minimum"):
        raise _ChartError(f"Error: the result is too large to chart; aggregate in SQL to at most {MAX_DATA_ROWS} rows.")
    rows: list[list[Any]] = result["rows"]
    indexes = {name: columns.index(name) for name in used}
    numeric = set(args.numeric_columns())
    # The UI puts scatter points on a value axis only when every x is a number.
    if args.type == "scatter" and _all_numeric(rows, indexes[args.x]):
        numeric.add(args.x)
    return [
        {name: _to_number(row[index]) if name in numeric else row[index] for name, index in indexes.items()}
        for row in rows
    ]


def _pivot(records: list[dict[str, Any]], x: str, series: str, value: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Long-format rows to one row per ``x`` with a column per ``series`` value, in first-seen order."""
    names: dict[str, None] = {}
    by_x: dict[Any, dict[str, Any]] = {}
    for record in records:
        name = _NULL_SERIES if record[series] is None else str(record[series])
        names.setdefault(name, None)
        if len(names) > MAX_SERIES:
            raise _ChartError(f"Error: `{series}` has more than {MAX_SERIES} values; filter or group them in SQL.")
        row = by_x.setdefault(record[x], {x: record[x]})
        if name in row:
            raise _ChartError(f"Error: rows repeat an `{x}` and `{series}` pair; aggregate them with GROUP BY.")
        row[name] = record[value]
    if x in names:
        raise _ChartError(f"Error: a `{series}` value is named like the x column `{x}`; rename the column.")
    rows = list(by_x.values())
    for row in rows:
        for name in names:
            row.setdefault(name, None)
    return rows, list(names)


def _flows(records: list[dict[str, Any]], source: str, target: str, value: str) -> list[dict[str, Any]]:
    """One row per source/target pair with its amounts summed; rows without a name or an amount are dropped."""
    totals: dict[tuple[str, str], float] = {}
    for record in records:
        amount = record[value]
        if record[source] is None or record[target] is None or not amount:
            continue
        if amount < 0:
            raise _ChartError(f"Error: `{value}` has negative amounts; a sankey needs flows above 0.")
        pair = (str(record[source]), str(record[target]))
        totals[pair] = totals.get(pair, 0.0) + amount
    return [{source: start, target: end, value: amount} for (start, end), amount in totals.items()]


def _rows_and_series(records: list[dict[str, Any]], args: ChartArgs) -> tuple[list[dict[str, Any]], list[str]]:
    if args.type == "sankey" and args.target:
        return _flows(records, args.x, args.target, args.y[0]), args.y
    if args.series:
        return _pivot(records, args.x, args.series, args.y[0])
    return records, args.y


def _chart_data(result: dict[str, Any], args: ChartArgs) -> tuple[list[dict[str, Any]], list[str]]:
    """The rows to plot and the ``y`` series names."""
    rows, y = _rows_and_series(_records(result, args), args)
    if len(rows) > MAX_DATA_ROWS:
        raise _ChartError(f"Error: the chart would have {len(rows)} rows; aggregate in SQL to at most {MAX_DATA_ROWS}.")
    if not rows:
        raise _ChartError("Error: the query returned no rows to chart.")
    return rows, y


def _axis_options(axis: _AxisArgs | None) -> YAxisOptions | None:
    return cast(YAxisOptions, axis.model_dump(exclude_none=True)) if axis else None


def _render(args: ChartArgs, rows: list[dict[str, Any]], y: list[str]) -> str:
    return render_chart(
        chart_type=args.type,
        data=rows,
        x=args.x,
        y=y,
        title=args.title,
        x_type=args.x_type,
        x_name=args.x_name,
        y_axis=_axis_options(args.y_axis),
        stacked=args.stacked,
        horizontal=args.horizontal,
        y2=args.y2,
        y2_axis=_axis_options(args.y2_axis),
        size=args.size,
        target=args.target,
        reference_lines=[cast(ReferenceLine, line.model_dump(exclude_none=True)) for line in args.reference_lines],
        reference_bands=[
            cast(ReferenceBand, band.model_dump(exclude_none=True, by_alias=True)) for band in args.reference_bands
        ],
    )


def _format_x(value: Any) -> str:
    return f"{value:.6g}" if isinstance(value, float) else str(value)


def _series_line(rows: list[dict[str, Any]], x: str, name: str, label: str) -> str:
    """Min and max with the x value where each occurs, and the last value."""
    points = [(row[x], row[name]) for row in rows if row[name] is not None]
    if not points:
        return f"- {label}: no values"
    low = min(points, key=itemgetter(1))
    high = max(points, key=itemgetter(1))
    return (
        f"- {label}: min {low[1]:.6g} ({_format_x(low[0])}), max {high[1]:.6g} ({_format_x(high[0])}), "
        f"last {points[-1][1]:.6g}"
    )


def _x_range(rows: list[dict[str, Any]], x: str) -> str:
    values = [row[x] for row in rows]
    if all(isinstance(value, float) for value in values):
        return f"`{x}` from {min(values):.6g} to {max(values):.6g}"
    return f"`{x}` from {values[0]} to {values[-1]}"


def _header(args: ChartArgs) -> str:
    title = f' "{args.title}"' if args.title else ""
    return f"Chart shown to the user: {args.type}{title}"


def _sankey_summary(args: ChartArgs, rows: list[dict[str, Any]], target: str) -> str:
    """Node and flow counts, the total leaving the start nodes and the largest flows."""
    value = args.y[0]
    targets = {row[target] for row in rows}
    nodes = targets | {row[args.x] for row in rows}
    total = sum(row[value] for row in rows if row[args.x] not in targets)
    largest = sorted(rows, key=itemgetter(value), reverse=True)[:_LARGEST_FLOWS]
    flows = "; ".join(f"{row[args.x]} → {row[target]} {row[value]:.6g}" for row in largest)
    return (
        f"{_header(args)}, {len(nodes)} nodes, {len(rows)} flows, {total:.6g} leaving the start nodes.\n"
        f"- largest flows: {flows}"
    )


def _summary(args: ChartArgs, rows: list[dict[str, Any]], y: list[str]) -> str:
    if args.type == "sankey" and args.target:
        return _sankey_summary(args, rows, args.target)
    header = f"{_header(args)}, {len(rows)} rows, {_x_range(rows, args.x)}."
    lines = [_series_line(rows, args.x, name, name) for name in [*y, *(args.y2 or [])]]
    if args.size:
        lines.append(_series_line(rows, args.x, args.size, f"{args.size} (bubble size)"))
    return "\n".join([header, *lines])


class ChartBuildError(Exception):
    """A message for the model about a chart that could not be built."""


@dataclass(frozen=True)
class ChartSource:
    """The read-only SQL tool charts rerun their query through."""

    server: FilteredMCPServerStreamableHttp
    tool_name: str


@dataclass(frozen=True)
class BuiltChart:
    """A chart's ``echarts-chart`` fence for the user and its summary for the model."""

    fence: str
    summary: str


async def build_chart(source: ChartSource, args: ChartArgs, ctx: Any) -> BuiltChart:
    """Rerun *args*' query as the user and build its chart; raises :class:`ChartBuildError`."""
    try:
        result = await rerun_query(source.server, source.tool_name, args.query, ctx)
        rows, y = _chart_data(result, args)
        fence = _render(args, rows, y)
    except (RerunError, _ChartError) as exc:
        raise ChartBuildError(str(exc)) from exc
    except ValueError as exc:
        raise ChartBuildError(f"Error: {exc}") from exc
    return BuiltChart(fence=fence, summary=_summary(args, rows, y))


def find_chart_source(mcp_servers: Sequence[object]) -> ChartSource | None:
    """The read-only SQL tool when the UI renders charts and the tool is mounted."""
    if not env.read_bool(env.ENABLE_CHARTS):
        return None
    found = find_read_only_sql_tool(mcp_servers)
    return ChartSource(*found) if found else None


@dataclass(frozen=True)
class ChartTool:
    """Reruns the query through the read-only SQL tool and files the chart for the user."""

    source: ChartSource

    async def __call__(self, ctx: Any, args_json: str) -> str:
        try:
            args = ChartArgs.model_validate_json(args_json or "{}")
        except ValidationError as exc:
            return invalid_args_message(exc, _USAGE)
        outbox = ui_outbox(ctx)
        if outbox is None:
            return _NO_UI
        try:
            chart = await build_chart(self.source, args, ctx)
        except ChartBuildError as exc:
            return str(exc)
        ui_parts, call_id = outbox
        ui_parts[call_id] = chart.fence
        return chart.summary


@dataclass(frozen=True)
class AgentChartTool:
    """This request's ``_chart`` tool and its guidance."""

    tools: tuple[FunctionTool, ...]
    guidance: str


def prepare_chart_tool(mcp_servers: Sequence[object]) -> AgentChartTool | None:
    """The tool and guidance when the UI renders charts and the read-only SQL tool is mounted."""
    source = find_chart_source(mcp_servers)
    if source is None:
        return None
    tool = FunctionTool(
        name=CHART_TOOL_NAME,
        description=_DESCRIPTION,
        params_json_schema=CHART_PARAMS,
        on_invoke_tool=ChartTool(source),
        strict_json_schema=False,
    )
    return AgentChartTool(tools=(tool,), guidance=_GUIDANCE)
