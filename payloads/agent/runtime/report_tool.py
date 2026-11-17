"""Show a Markdown report to the user: ``_report``.

The model passes a title and the report as Markdown.  The tool builds a
``markdown-report`` fence with :func:`render_report` and files it in the
request's :attr:`RequestState.ui_parts`, so the output path shows it right
after the tool result.  The chat UI renders the fence as a report card the
user can copy or download as ``.md`` or HTML.  Offered when reports
are enabled in the UI.

When the UI renders charts and the read-only SQL tool is mounted, the
report can carry charts: each one names a query like ``_chart`` does and is
placed with ``{{chart:<id>}}``.  The tool reruns the query as the user and
puts the ``echarts-chart`` fence there, so the rows never reach the model.

- Frontend renderer: ``payloads/agent/ui/overrides/client/src/components/elements/report-block.tsx``
- Fence parsing (must stay in sync): ``payloads/agent/ui/overrides/client/src/components/elements/report-fence.ts``
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from agents import FunctionTool
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from agent_foundry import env
from payloads.agent.runtime.chart_tool import (
    CHART_PARAMS,
    CHART_TOOL_NAME,
    BuiltChart,
    ChartArgs,
    ChartBuildError,
    ChartSource,
    build_chart,
    find_chart_source,
)
from payloads.agent.runtime.request_context import ui_outbox
from payloads.agent.runtime.sql_rerun import invalid_args_message

REPORT_TOOL_NAME: Final = "_report"
REPORT_FENCE_TAG: Final = "markdown-report"
MAX_REPORT_CHARS: Final = 100_000
MAX_TITLE_CHARS: Final = 200
MAX_REPORT_CHARTS: Final = 10
MAX_FILENAME_CHARS: Final = 80
DEFAULT_FILENAME: Final = "report"
# Four backticks so the report's own three-backtick code blocks stay inside it.
_MIN_FENCE_LENGTH: Final = 4
_BACKTICK_RUN: Final = re.compile(r"`+")
_NOT_FILENAME_CHARS: Final = re.compile(r"[^a-z0-9]+")
_CHART_ID: Final = r"[a-z0-9][a-z0-9_-]{0,39}"
_CHART_PLACEHOLDER: Final = re.compile(r"\{\{chart:(" + _CHART_ID + r")\}\}")

_GUIDANCE: Final = (
    "## Reports\n"
    f"- When the user asks for a report, a write-up or another document they will keep or share, call "
    f"`{REPORT_TOOL_NAME}` with a title and the whole report as Markdown. The user sees it as a card they can "
    "download as a `.md` or HTML file. Start the report with a `# ` heading.\n"
    "- Answer ordinary questions in the chat, not as a report.\n"
    "- After the tool returns, add a sentence or two at most. Don't repeat the report.\n"
    f"- To change a report, call `{REPORT_TOOL_NAME}` again with the whole updated report."
)
_CHART_GUIDANCE: Final = (
    "\n- To put charts in a report, pass them in `charts`, each with an `id` and the same fields as "
    f"`{CHART_TOOL_NAME}`, and place each one on its own line with `{{{{chart:<id>}}}}`. The tool runs the "
    "queries, so the rows never pass through you. Use Mermaid only for diagrams such as flowcharts, not for "
    "charts of data."
)
_DESCRIPTION: Final = (
    "Show a Markdown report to the user as a card they can copy or download as a .md or HTML file. Pass "
    f"the whole report; at most {MAX_REPORT_CHARS:,} characters."
)
_CHART_DESCRIPTION: Final = (
    f" Optional `charts` (up to {MAX_REPORT_CHARTS}) run a read-only SQL query each, as the user, and appear "
    "where the markdown says `{{chart:<id>}}`; you get a short summary per chart, not the rows."
)
_BASE_PROPERTIES: Final[dict[str, Any]] = {
    "title": {"type": "string", "minLength": 1, "maxLength": MAX_TITLE_CHARS, "description": "Names the file."},
    "markdown": {"type": "string", "minLength": 1, "maxLength": MAX_REPORT_CHARS},
}
_CHART_ITEM_PARAMS: Final[dict[str, Any]] = {
    **CHART_PARAMS,
    "properties": {
        "id": {
            "type": "string",
            "pattern": f"^{_CHART_ID}$",
            "description": "Place the chart with {{chart:<id>}} in the markdown.",
        },
        **CHART_PARAMS["properties"],
    },
    "required": ["id", *CHART_PARAMS["required"]],
}
_USAGE: Final = (
    f"Give `title` (1 to {MAX_TITLE_CHARS} characters) and `markdown` (1 to {MAX_REPORT_CHARS:,} characters); "
    "each chart in `charts` needs a unique `id`, placed once with `{{chart:<id>}}`."
)
_NO_UI: Final = "Error: reports can't be shown in this conversation."
_NO_CHARTS: Final = "Error: charts can't be added to reports here; leave out `charts` and `{{chart:...}}`."


class _ReportChartArgs(ChartArgs):
    id: str = Field(pattern=f"^{_CHART_ID}$")


class _ReportArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=MAX_TITLE_CHARS)
    markdown: str = Field(min_length=1, max_length=MAX_REPORT_CHARS)
    charts: list[_ReportChartArgs] = Field(default_factory=list, max_length=MAX_REPORT_CHARTS)

    @model_validator(mode="after")
    def _each_chart_placed_once(self) -> _ReportArgs:
        ids = [chart.id for chart in self.charts]
        placed = _CHART_PLACEHOLDER.findall(self.markdown)
        if len(set(ids)) != len(ids):
            raise ValueError("chart ids must be unique")
        if len(set(placed)) != len(placed):
            raise ValueError("place each chart once")
        if missing := sorted(set(placed) - set(ids)):
            raise ValueError(f"no chart in `charts` for {', '.join(missing)}")
        if unplaced := sorted(set(ids) - set(placed)):
            raise ValueError(f"place {', '.join(unplaced)} in the markdown with {{{{chart:<id>}}}}")
        return self


def report_filename(title: str) -> str:
    """*title* as a file stem: lower-case letters and digits joined by single hyphens."""
    stem = _NOT_FILENAME_CHARS.sub("-", title.lower()).strip("-")[:MAX_FILENAME_CHARS].rstrip("-")
    return stem or DEFAULT_FILENAME


def render_report(title: str, markdown: str) -> str:
    """A ``markdown-report`` fence holding *markdown*, longer than any backtick run inside it."""
    longest_run = max((len(run) for run in _BACKTICK_RUN.findall(markdown)), default=0)
    fence = "`" * max(_MIN_FENCE_LENGTH, longest_run + 1)
    body = markdown.strip("\n")
    return f"{fence}{REPORT_FENCE_TAG}:{report_filename(title)}\n{body}\n{fence}"


def place_charts(markdown: str, fences: dict[str, str]) -> str:
    """*markdown* with each ``{{chart:<id>}}`` replaced by that chart's fence, as its own block."""
    return _CHART_PLACEHOLDER.sub(lambda match: f"\n\n{fences[match.group(1)]}\n\n", markdown)


async def _build_charts(source: ChartSource, charts: list[_ReportChartArgs], ctx: Any) -> dict[str, BuiltChart]:
    """Each chart by id, built in order; the first failure names its chart."""
    built: dict[str, BuiltChart] = {}
    for chart in charts:
        try:
            built[chart.id] = await build_chart(source, chart, ctx)
        except ChartBuildError as exc:
            raise ChartBuildError(f"Chart `{chart.id}`: {exc}") from exc
    return built


def _summary(args: _ReportArgs, charts: dict[str, BuiltChart]) -> str:
    header = f'Report shown to the user: "{args.title}", {len(args.markdown):,} characters'
    if not charts:
        return f"{header}."
    count = f"{len(charts)} chart" if len(charts) == 1 else f"{len(charts)} charts"
    lines = [f"`{chart_id}`: {chart.summary}" for chart_id, chart in charts.items()]
    return "\n".join([f"{header}, {count}.", *lines])


@dataclass(frozen=True)
class ReportTool:
    """Files the report, with its charts, for the user and tells the model it was shown."""

    chart_source: ChartSource | None = None

    async def __call__(self, ctx: Any, args_json: str) -> str:
        try:
            args = _ReportArgs.model_validate_json(args_json or "{}")
        except ValidationError as exc:
            return invalid_args_message(exc, _USAGE)
        outbox = ui_outbox(ctx)
        if outbox is None:
            return _NO_UI
        charts: dict[str, BuiltChart] = {}
        if args.charts:
            if self.chart_source is None:
                return _NO_CHARTS
            try:
                charts = await _build_charts(self.chart_source, args.charts, ctx)
            except ChartBuildError as exc:
                return str(exc)
        markdown = place_charts(args.markdown, {chart_id: chart.fence for chart_id, chart in charts.items()})
        ui_parts, call_id = outbox
        ui_parts[call_id] = render_report(args.title, markdown)
        return _summary(args, charts)


@dataclass(frozen=True)
class AgentReportTool:
    """This request's ``_report`` tool and its guidance."""

    tools: tuple[FunctionTool, ...]
    guidance: str


def _params(with_charts: bool) -> dict[str, Any]:
    properties = dict(_BASE_PROPERTIES)
    if with_charts:
        properties["charts"] = {"type": "array", "maxItems": MAX_REPORT_CHARTS, "items": _CHART_ITEM_PARAMS}
    return {
        "type": "object",
        "properties": properties,
        "required": ["title", "markdown"],
        "additionalProperties": False,
    }


def prepare_report_tool(mcp_servers: Sequence[object]) -> AgentReportTool | None:
    """The tool and guidance when the UI renders reports; with charts when ``_chart`` could run."""
    if not env.read_bool(env.ENABLE_REPORTS):
        return None
    chart_source = find_chart_source(mcp_servers)
    with_charts = chart_source is not None
    tool = FunctionTool(
        name=REPORT_TOOL_NAME,
        description=_DESCRIPTION + (_CHART_DESCRIPTION if with_charts else ""),
        params_json_schema=_params(with_charts),
        on_invoke_tool=ReportTool(chart_source),
        strict_json_schema=False,
    )
    return AgentReportTool(tools=(tool,), guidance=_GUIDANCE + (_CHART_GUIDANCE if with_charts else ""))
