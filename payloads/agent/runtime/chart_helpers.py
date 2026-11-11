"""
Chart Markdown Helpers
=======================
Agent-side helper for emitting ``echarts-chart`` markdown fences that
the chat UI's fence-dispatcher renders as interactive ECharts charts.

See:
- Frontend renderer: ``payloads/agent/ui/overrides/client/src/components/elements/interactive-chart.tsx``
- Schema (must stay in sync): ``payloads/agent/ui/overrides/client/src/components/elements/chart-spec.ts``
- Override ledger: ``payloads/agent/ui/overrides/MANIFEST.md`` "Fence-dispatch contract"

Example::

    from payloads.agent.runtime.chart_helpers import render_chart

    md = render_chart(
        chart_type="bar",
        data=[{"q": "Q1", "rev": 120}, {"q": "Q2", "rev": 150}],
        x="q",
        y=["rev"],
        title="Revenue by Quarter",
    )
    # `md` is a markdown fenced block ready to embed in an agent response.
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Sequence

# Mirrors ``CHART_TYPES`` in ``chart-spec.ts``.  Keep in sync.
SUPPORTED_CHART_TYPES: frozenset[str] = frozenset(
    {"bar", "line", "area", "pie", "scatter"},
)
MAX_DATA_ROWS = 500


def _validate_chart_type(chart_type: str) -> None:
    if chart_type not in SUPPORTED_CHART_TYPES:
        supported = ", ".join(sorted(SUPPORTED_CHART_TYPES))
        raise ValueError(
            f"Unsupported chart_type {chart_type!r}; expected one of: {supported}",
        )


def _validate_data(data: Sequence[dict[str, Any]]) -> None:
    if not isinstance(data, (list, tuple)):
        raise ValueError(
            f"data must be a list of dicts; got {type(data).__name__}",
        )
    if len(data) > MAX_DATA_ROWS:
        raise ValueError(
            f"data has {len(data)} rows; the renderer caps at {MAX_DATA_ROWS}. "
            "Aggregate or downsample before calling render_chart.",
        )
    for i, row in enumerate(data):
        if not isinstance(row, dict):
            raise ValueError(
                f"data[{i}] must be a dict; got {type(row).__name__}",
            )


def _validate_axes(
    data: Sequence[dict[str, Any]],
    x: str,
    y: Iterable[str],
) -> list[str]:
    if not isinstance(x, str) or not x:
        raise ValueError("x must be a non-empty string field name")
    y_list = list(y)
    if not y_list or not all(isinstance(field, str) and field for field in y_list):
        raise ValueError("y must be a non-empty list of field names")
    if data:
        first = data[0]
        for field in (x, *y_list):
            if field not in first:
                raise ValueError(
                    f"field {field!r} is not present in the first data row (available: {sorted(first.keys())})",
                )
    return y_list


def render_chart(
    *,
    chart_type: str,
    data: Sequence[dict[str, Any]],
    x: str,
    y: Iterable[str],
    title: str | None = None,
    palette: Sequence[str] | None = None,
) -> str:
    """Build an ``echarts-chart`` markdown fence.

    The schema is wide-format only: one row per ``x`` value, one
    column per series listed in ``y``.  For multi-series charts,
    pivot your data (typically with a SQL ``GROUP BY`` + ``CASE``
    or pivot) before calling this helper.

    Validates inputs eagerly so authors see a clear Python error at
    emit time rather than a silent fallback to the upstream code
    block in the UI.

    Returns the markdown block as a string.  Embed it directly in
    the agent's response (it is rendered inline by Streamdown).

    Raises:
        ValueError: if ``chart_type`` is unsupported, ``data``
            exceeds :data:`MAX_DATA_ROWS`, or ``x`` / ``y`` reference
            fields missing from the first data row.
    """
    _validate_chart_type(chart_type)
    _validate_data(data)
    y_list = _validate_axes(data, x, y)

    spec: dict[str, Any] = {
        "type": chart_type,
        "data": list(data),
        "x": x,
        "y": y_list,
    }
    if title:
        spec["title"] = title
    if palette:
        spec["palette"] = list(palette)

    payload = json.dumps(spec, sort_keys=True, ensure_ascii=False)
    return f"```echarts-chart\n{payload}\n```"
