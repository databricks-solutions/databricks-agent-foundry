"""
Chart Markdown Helpers
=======================
Agent-side helper for emitting ``echarts-chart`` markdown fences that
the chat UI's fence-dispatcher renders as interactive ECharts charts.

See:
- Frontend renderer: ``payloads/agent/ui/overrides/client/src/components/elements/interactive-chart.tsx``
- Schema (must stay in sync): ``payloads/agent/ui/overrides/client/src/components/elements/chart-spec.ts``
  and chart types in ``chart-option.ts``
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
import math
from typing import Any, Iterable, Literal, NotRequired, Sequence, TypedDict

# Mirrors ``CHART_TYPES`` / ``STACKABLE_TYPES`` / ``HORIZONTAL_TYPES`` /
# ``MAX_SANKEY_NODES`` in ``chart-option.ts`` and the limits in
# ``chart-spec.ts``.  Keep in sync.
SUPPORTED_CHART_TYPES: frozenset[str] = frozenset(
    {"bar", "line", "area", "pie", "scatter", "sankey"},
)
MAX_SANKEY_NODES = 40
STACKABLE_CHART_TYPES: frozenset[str] = frozenset({"bar", "area"})
HORIZONTAL_CHART_TYPES: frozenset[str] = frozenset({"bar"})
X_TYPES: frozenset[str] = frozenset({"category", "time"})
MAX_DATA_ROWS = 500
MAX_AXIS_NAME_LENGTH = 60
MAX_REFERENCE_MARKS = 5

XType = Literal["category", "time"]
MarkAxis = Literal["x", "y"]


class YAxisOptions(TypedDict, total=False):
    """Value-axis settings; mirrors ``yAxisSchema`` in ``chart-spec.ts``."""

    name: str
    log: bool
    min: float
    max: float


class ReferenceLine(TypedDict):
    """``axis`` names the data axis: ``x`` = category/time, ``y`` = value."""

    axis: MarkAxis
    value: float | str
    label: NotRequired[str]


# Functional form because ``from`` is a Python keyword.
ReferenceBand = TypedDict(
    "ReferenceBand",
    {"axis": MarkAxis, "from": float | str, "to": float | str, "label": NotRequired[str]},
)

_Y_AXIS_KEYS: frozenset[str] = frozenset(YAxisOptions.__annotations__)
_LINE_KEYS: frozenset[str] = frozenset({"axis", "value", "label"})
_BAND_KEYS: frozenset[str] = frozenset({"axis", "from", "to", "label"})
_MARK_AXES: frozenset[str] = frozenset({"x", "y"})


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


def _validate_field_list(label: str, fields: Iterable[str]) -> list[str]:
    field_list = list(fields)
    if not field_list or not all(isinstance(field, str) and field for field in field_list):
        raise ValueError(f"{label} must be a non-empty list of field names")
    return field_list


def _validate_fields_present(data: Sequence[dict[str, Any]], fields: Iterable[str]) -> None:
    if not data:
        return
    first = data[0]
    for field in fields:
        if field not in first:
            raise ValueError(
                f"field {field!r} is not present in the first data row (available: {sorted(first.keys())})",
            )


def _validate_axes(
    data: Sequence[dict[str, Any]],
    x: str,
    y: Iterable[str],
) -> list[str]:
    if not isinstance(x, str) or not x:
        raise ValueError("x must be a non-empty string field name")
    y_list = _validate_field_list("y", y)
    _validate_fields_present(data, (x, *y_list))
    return y_list


def _validate_axis_name(label: str, name: str | None) -> None:
    if name is not None and len(name) > MAX_AXIS_NAME_LENGTH:
        raise ValueError(f"{label} must be at most {MAX_AXIS_NAME_LENGTH} characters")


def _validate_value_axis(label: str, axis: YAxisOptions | None) -> None:
    if axis is None:
        return
    unknown = set(axis) - _Y_AXIS_KEYS
    if unknown:
        raise ValueError(f"{label} has unsupported keys: {sorted(unknown)}")
    _validate_axis_name(f"{label} name", axis.get("name"))
    low, high = axis.get("min"), axis.get("max")
    if axis.get("log") and low is not None and low <= 0:
        raise ValueError(f"{label} min must be greater than 0 on a log axis")
    if low is not None and high is not None and low >= high:
        raise ValueError(f"{label} min must be less than {label} max")


def _validate_y2(
    chart_type: str,
    y_list: Sequence[str],
    y2_list: Sequence[str] | None,
    y2_axis: YAxisOptions | None,
    stacked: bool,
    horizontal: bool,
) -> None:
    if y2_list is None:
        if y2_axis is not None:
            raise ValueError("y2_axis requires y2")
        return
    if chart_type == "pie":
        raise ValueError("y2 is not supported for pie")
    if horizontal:
        raise ValueError("y2 does not support horizontal")
    if stacked:
        raise ValueError("y2 does not support stacked")
    if set(y2_list) & set(y_list):
        raise ValueError("y2 fields must not repeat y fields")
    _validate_value_axis("y2_axis", y2_axis)


def _validate_mark_shape(label: str, mark: dict[str, Any], keys: frozenset[str]) -> None:
    if not isinstance(mark, dict):
        raise ValueError(f"{label} entries must be dicts; got {type(mark).__name__}")
    unknown = set(mark) - keys
    if unknown:
        raise ValueError(f"{label} entry has unsupported keys: {sorted(unknown)}")
    missing = (keys - {"label"}) - set(mark)
    if missing:
        raise ValueError(f"{label} entry is missing keys: {sorted(missing)}")
    if mark["axis"] not in _MARK_AXES:
        raise ValueError(f"{label} axis must be 'x' or 'y'; got {mark['axis']!r}")
    _validate_axis_name(f"{label} label", mark.get("label"))


def _validate_value_marks(label: str, values: Sequence[Any], log_axis: bool) -> None:
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in values):
        raise ValueError(f"{label} on the y axis must be numeric")
    if log_axis and any(value <= 0 for value in values):
        raise ValueError(f"{label} on the y axis must be greater than 0 on a log axis")


def _validate_references(
    chart_type: str,
    lines: Sequence[ReferenceLine],
    bands: Sequence[ReferenceBand],
    log_axis: bool,
) -> None:
    if not lines and not bands:
        return
    if chart_type == "pie":
        raise ValueError("reference lines and bands are not supported for pie")
    if len(lines) > MAX_REFERENCE_MARKS or len(bands) > MAX_REFERENCE_MARKS:
        raise ValueError(f"at most {MAX_REFERENCE_MARKS} reference lines and {MAX_REFERENCE_MARKS} bands")
    for line in lines:
        _validate_mark_shape("reference_lines", dict(line), _LINE_KEYS)
    for band in bands:
        _validate_mark_shape("reference_bands", dict(band), _BAND_KEYS)
    y_lines = [line["value"] for line in lines if line["axis"] == "y"]
    y_bands = [bound for band in bands if band["axis"] == "y" for bound in (band["from"], band["to"])]
    _validate_value_marks("reference line values", y_lines, log_axis)
    _validate_value_marks("reference band bounds", y_bands, log_axis)
    if any(_is_reversed(band) for band in bands):
        raise ValueError("reference band from must be less than to")


def _is_reversed(band: ReferenceBand) -> bool:
    low, high = band["from"], band["to"]
    return isinstance(low, (int, float)) and isinstance(high, (int, float)) and low >= high


def _validate_size(chart_type: str, size: str | None) -> None:
    if size is None:
        return
    if not isinstance(size, str) or not size:
        raise ValueError("size must be a non-empty field name")
    if chart_type != "scatter":
        raise ValueError("size requires chart_type 'scatter'")


def _is_positive(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and number > 0


def _flow_pairs(data: Sequence[dict[str, Any]], source: str, target: str, value: str) -> list[tuple[str, str]]:
    """Distinct source/target pairs of the rows a sankey draws, like ``sankeyLinks`` in ``chart-option.ts``."""
    pairs = [
        (str(row[source]), str(row[target]))
        for row in data
        if row.get(source) is not None and row.get(target) is not None and _is_positive(row.get(value))
    ]
    return list(dict.fromkeys(pairs))


def looping_nodes(pairs: Sequence[tuple[str, str]]) -> list[str]:
    """Nodes on a loop: peel off flows from nodes nothing flows into and to nodes nothing flows out of."""
    remaining = list(pairs)
    while True:
        targets = {target for _, target in remaining}
        sources = {source for source, _ in remaining}
        kept = [(source, target) for source, target in remaining if source in targets and target in sources]
        if len(kept) == len(remaining):
            return sorted({name for pair in kept for name in pair})
        remaining = kept


def _validate_flows(pairs: Sequence[tuple[str, str]]) -> None:
    node_count = len({name for pair in pairs for name in pair})
    if node_count > MAX_SANKEY_NODES:
        raise ValueError(
            f"sankey has {node_count} nodes; at most {MAX_SANKEY_NODES}. Group small ones into 'Other' in SQL.",
        )
    looping = looping_nodes(pairs)
    if looping:
        raise ValueError(
            f"sankey flows must not loop back ({', '.join(looping)}); "
            "prefix node names with their stage if a name repeats across stages.",
        )


def _validate_sankey(
    chart_type: str,
    data: Sequence[dict[str, Any]],
    x: str,
    target: str | None,
    y_list: list[str],
    unsupported: dict[str, Any],
) -> None:
    if target is None:
        if chart_type == "sankey":
            raise ValueError("sankey requires target")
        return
    if chart_type != "sankey":
        raise ValueError("target requires chart_type 'sankey'")
    if target == x:
        raise ValueError("target must differ from x")
    if len(y_list) != 1:
        raise ValueError("sankey takes exactly one y")
    used = [name for name, value in unsupported.items() if value]
    if used:
        raise ValueError(f"sankey does not support {', '.join(used)}")
    _validate_fields_present(data, [target])
    _validate_flows(_flow_pairs(data, x, target, y_list[0]))


def _validate_layout(
    chart_type: str,
    x_type: str | None,
    stacked: bool,
    horizontal: bool,
    log_axis: bool,
) -> None:
    if x_type is not None and x_type not in X_TYPES:
        raise ValueError(f"x_type must be one of {sorted(X_TYPES)}; got {x_type!r}")
    if horizontal and chart_type not in HORIZONTAL_CHART_TYPES:
        raise ValueError("horizontal requires chart_type 'bar'")
    if horizontal and x_type == "time":
        raise ValueError("horizontal does not support x_type 'time'")
    if stacked and chart_type not in STACKABLE_CHART_TYPES:
        raise ValueError("stacked requires chart_type 'bar' or 'area'")
    if stacked and log_axis:
        raise ValueError("stacked does not support a log y_axis")
    if x_type == "time" and chart_type == "pie":
        raise ValueError("x_type 'time' is not supported for pie")


def render_chart(
    *,
    chart_type: str,
    data: Sequence[dict[str, Any]],
    x: str,
    y: Iterable[str],
    title: str | None = None,
    palette: Sequence[str] | None = None,
    x_type: XType | None = None,
    x_name: str | None = None,
    y_axis: YAxisOptions | None = None,
    stacked: bool = False,
    horizontal: bool = False,
    y2: Iterable[str] | None = None,
    y2_axis: YAxisOptions | None = None,
    size: str | None = None,
    target: str | None = None,
    reference_lines: Sequence[ReferenceLine] = (),
    reference_bands: Sequence[ReferenceBand] = (),
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

    ``x_type="time"`` places ISO date strings on a time scale (gaps
    stay visible); ``stacked`` applies to bar/area, ``horizontal`` to
    bar.  ``y2`` series use a right-hand axis configured by
    ``y2_axis``; ``size`` turns a scatter chart into a bubble chart.
    Reference lines and bands are measured against the left axis.

    ``chart_type="sankey"`` draws flows: ``x`` is the source column,
    ``target`` the destination column and the single ``y`` the amount.
    Repeated pairs are summed in the UI; at most
    :data:`MAX_SANKEY_NODES` nodes, and flows must not loop back.

    Raises:
        ValueError: if ``chart_type`` is unsupported, ``data``
            exceeds :data:`MAX_DATA_ROWS`, ``x`` / ``y`` / ``y2`` /
            ``size`` reference fields missing from the first data row,
            or the axis, layout and reference options conflict with
            each other or the chart type.
    """
    _validate_chart_type(chart_type)
    _validate_data(data)
    y_list = _validate_axes(data, x, y)
    y2_list = _validate_field_list("y2", y2) if y2 is not None else None
    _validate_size(chart_type, size)
    _validate_fields_present(data, [*(y2_list or []), *([size] if size else [])])
    _validate_axis_name("x_name", x_name)
    _validate_value_axis("y_axis", y_axis)
    log_axis = bool(y_axis and y_axis.get("log"))
    _validate_layout(chart_type, x_type, stacked, horizontal, log_axis=log_axis)
    _validate_y2(chart_type, y_list, y2_list, y2_axis, stacked, horizontal)
    _validate_references(chart_type, reference_lines, reference_bands, log_axis)
    unsupported_by_sankey = {
        "x_type": x_type,
        "x_name": x_name,
        "y_axis": y_axis,
        "y2": y2_list,
        "y2_axis": y2_axis,
        "reference_lines": reference_lines,
        "reference_bands": reference_bands,
    }
    _validate_sankey(chart_type, data, x, target, y_list, unsupported_by_sankey)

    spec: dict[str, Any] = {
        "type": chart_type,
        "data": list(data),
        "x": x,
        "y": y_list,
    }
    optional: dict[str, Any] = {
        "title": title,
        "palette": list(palette) if palette else None,
        "xType": x_type,
        "xName": x_name,
        "yAxis": dict(y_axis) if y_axis else None,
        "stacked": stacked,
        "horizontal": horizontal,
        "y2": y2_list,
        "y2Axis": dict(y2_axis) if y2_axis else None,
        "size": size,
        "target": target,
        "referenceLines": [dict(line) for line in reference_lines],
        "referenceBands": [dict(band) for band in reference_bands],
    }
    spec.update({key: value for key, value in optional.items() if value})

    payload = json.dumps(spec, sort_keys=True, ensure_ascii=False)
    return f"```echarts-chart\n{payload}\n```"
