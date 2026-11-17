"""Tests for ``payloads.agent.runtime.chart_helpers``."""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from payloads.agent.runtime.chart_helpers import (
    MAX_AXIS_NAME_LENGTH,
    MAX_DATA_ROWS,
    MAX_REFERENCE_MARKS,
    MAX_SANKEY_NODES,
    SUPPORTED_CHART_TYPES,
    looping_nodes,
    render_chart,
)

SAMPLE_DATA = [
    {"q": "Q1", "rev": 120},
    {"q": "Q2", "rev": 150},
]


def _extract_payload(block: str) -> dict[str, Any]:
    match = re.fullmatch(
        r"```echarts-chart\n(.*)\n```",
        block,
        flags=re.DOTALL,
    )
    assert match, f"unexpected fence shape:\n{block}"
    payload: dict[str, Any] = json.loads(match.group(1))
    return payload


class TestRenderChartHappyPath:
    @pytest.mark.parametrize("chart_type", sorted(SUPPORTED_CHART_TYPES - {"sankey"}))
    def test_supported_chart_types(self, chart_type: str) -> None:
        block = render_chart(
            chart_type=chart_type,
            data=SAMPLE_DATA,
            x="q",
            y=["rev"],
        )
        payload = _extract_payload(block)
        assert payload["type"] == chart_type
        assert payload["data"] == SAMPLE_DATA
        assert payload["x"] == "q"
        assert payload["y"] == ["rev"]

    def test_optional_fields_round_trip(self) -> None:
        block = render_chart(
            chart_type="line",
            data=SAMPLE_DATA,
            x="q",
            y=["rev"],
            title="Revenue",
            palette=["#111", "#222"],
        )
        payload = _extract_payload(block)
        assert payload["title"] == "Revenue"
        assert payload["palette"] == ["#111", "#222"]

    def test_omitted_optional_fields_absent(self) -> None:
        block = render_chart(
            chart_type="bar",
            data=SAMPLE_DATA,
            x="q",
            y=["rev"],
        )
        payload = _extract_payload(block)
        for omitted in ("title", "palette"):
            assert omitted not in payload

    def test_payload_has_stable_key_order(self) -> None:
        block_a = render_chart(
            chart_type="bar",
            data=SAMPLE_DATA,
            x="q",
            y=["rev"],
            title="Revenue",
        )
        block_b = render_chart(
            chart_type="bar",
            data=SAMPLE_DATA,
            x="q",
            y=["rev"],
            title="Revenue",
        )
        assert block_a == block_b

    def test_unicode_round_trips(self) -> None:
        block = render_chart(
            chart_type="bar",
            data=[{"q": "Q1 \u00e9", "rev": 1}],
            x="q",
            y=["rev"],
            title="Revenue \u00fc",
        )
        payload = _extract_payload(block)
        assert payload["title"] == "Revenue \u00fc"
        assert payload["data"][0]["q"] == "Q1 \u00e9"


class TestRenderChartValidation:
    def test_invalid_chart_type_raises(self) -> None:
        with pytest.raises(ValueError, match="Unsupported chart_type"):
            render_chart(
                chart_type="treemap",
                data=SAMPLE_DATA,
                x="q",
                y=["rev"],
            )

    def test_oversized_data_raises(self) -> None:
        oversized = [{"q": str(i), "rev": i} for i in range(MAX_DATA_ROWS + 1)]
        with pytest.raises(ValueError, match=f"caps at {MAX_DATA_ROWS}"):
            render_chart(
                chart_type="bar",
                data=oversized,
                x="q",
                y=["rev"],
            )

    def test_max_data_rows_accepted(self) -> None:
        at_limit = [{"q": str(i), "rev": i} for i in range(MAX_DATA_ROWS)]
        block = render_chart(
            chart_type="bar",
            data=at_limit,
            x="q",
            y=["rev"],
        )
        assert len(_extract_payload(block)["data"]) == MAX_DATA_ROWS

    def test_data_must_be_list(self) -> None:
        # Intentional negative test: ``render_chart`` rejects non-list
        # ``data`` at runtime; mypy's ``arg-type`` complaint is the
        # static mirror of the same constraint.
        with pytest.raises(ValueError, match="data must be a list"):
            render_chart(
                chart_type="bar",
                data={"q": "Q1"},  # type: ignore[arg-type]
                x="q",
                y=["rev"],
            )

    def test_row_must_be_dict(self) -> None:
        # Intentional negative test: ``render_chart`` rejects rows that
        # aren't dicts at runtime.
        with pytest.raises(ValueError, match=r"data\[0\] must be a dict"):
            render_chart(
                chart_type="bar",
                data=["Q1"],  # type: ignore[list-item]
                x="q",
                y=["rev"],
            )

    def test_empty_x_raises(self) -> None:
        with pytest.raises(ValueError, match="x must be a non-empty"):
            render_chart(
                chart_type="bar",
                data=SAMPLE_DATA,
                x="",
                y=["rev"],
            )

    def test_empty_y_raises(self) -> None:
        with pytest.raises(ValueError, match="y must be a non-empty"):
            render_chart(
                chart_type="bar",
                data=SAMPLE_DATA,
                x="q",
                y=[],
            )

    def test_x_field_missing_from_data_raises(self) -> None:
        with pytest.raises(ValueError, match="not present in the first data row"):
            render_chart(
                chart_type="bar",
                data=SAMPLE_DATA,
                x="missing",
                y=["rev"],
            )

    def test_y_field_missing_from_data_raises(self) -> None:
        with pytest.raises(ValueError, match="not present in the first data row"):
            render_chart(
                chart_type="bar",
                data=SAMPLE_DATA,
                x="q",
                y=["nope"],
            )

    def test_empty_data_skips_field_existence_check(self) -> None:
        # An empty data list is valid; the renderer just shows nothing.
        block = render_chart(
            chart_type="bar",
            data=[],
            x="q",
            y=["rev"],
        )
        assert _extract_payload(block)["data"] == []


class TestRenderChartAxesAndLayout:
    def test_axis_and_layout_options_round_trip(self) -> None:
        block = render_chart(
            chart_type="bar",
            data=SAMPLE_DATA,
            x="q",
            y=["rev"],
            x_type="category",
            x_name="Quarter",
            y_axis={"name": "EUR", "min": 1, "max": 1000},
            stacked=True,
            horizontal=True,
        )
        payload = _extract_payload(block)
        assert payload["xType"] == "category"
        assert payload["xName"] == "Quarter"
        assert payload["yAxis"] == {"name": "EUR", "min": 1, "max": 1000}
        assert payload["stacked"] is True
        assert payload["horizontal"] is True

    def test_time_axis_round_trips(self) -> None:
        block = render_chart(
            chart_type="line",
            data=[{"day": "2026-05-01", "count": 3}],
            x="day",
            y=["count"],
            x_type="time",
        )
        assert _extract_payload(block)["xType"] == "time"

    def test_unset_options_absent(self) -> None:
        payload = _extract_payload(render_chart(chart_type="bar", data=SAMPLE_DATA, x="q", y=["rev"]))
        for omitted in ("xType", "xName", "yAxis", "stacked", "horizontal"):
            assert omitted not in payload

    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"chart_type": "line", "horizontal": True}, "horizontal requires chart_type 'bar'"),
            ({"chart_type": "bar", "horizontal": True, "x_type": "time"}, "horizontal does not support"),
            ({"chart_type": "line", "stacked": True}, "stacked requires chart_type"),
            ({"chart_type": "bar", "stacked": True, "y_axis": {"log": True}}, "stacked does not support a log"),
            ({"chart_type": "pie", "x_type": "time"}, "not supported for pie"),
            ({"chart_type": "bar", "x_type": "value"}, "x_type must be one of"),
            ({"chart_type": "bar", "y_axis": {"log": True, "min": 0}}, "greater than 0 on a log axis"),
            ({"chart_type": "bar", "y_axis": {"min": 5, "max": 5}}, "min must be less than"),
            ({"chart_type": "bar", "y_axis": {"scale": True}}, "unsupported keys"),
            ({"chart_type": "bar", "x_name": "x" * (MAX_AXIS_NAME_LENGTH + 1)}, "x_name must be at most"),
        ],
    )
    def test_invalid_combinations_raise(self, kwargs: dict[str, Any], message: str) -> None:
        with pytest.raises(ValueError, match=message):
            render_chart(data=SAMPLE_DATA, x="q", y=["rev"], **kwargs)


DUAL_DATA = [
    {"q": "Q1", "rev": 120, "margin": 31, "aum": 10},
    {"q": "Q2", "rev": 150, "margin": 28, "aum": 30},
]


class TestRenderChartY2SizeAndReferences:
    def test_options_round_trip(self) -> None:
        block = render_chart(
            chart_type="line",
            data=DUAL_DATA,
            x="q",
            y=["rev"],
            y2=["margin"],
            y2_axis={"name": "%", "min": 0, "max": 100},
            reference_lines=[{"axis": "y", "value": 130, "label": "Target"}],
            reference_bands=[{"axis": "x", "from": "Q1", "to": "Q2"}],
        )
        payload = _extract_payload(block)
        assert payload["y2"] == ["margin"]
        assert payload["y2Axis"] == {"name": "%", "min": 0, "max": 100}
        assert payload["referenceLines"] == [{"axis": "y", "value": 130, "label": "Target"}]
        assert payload["referenceBands"] == [{"axis": "x", "from": "Q1", "to": "Q2"}]

    def test_bubble_size_round_trips(self) -> None:
        block = render_chart(chart_type="scatter", data=DUAL_DATA, x="rev", y=["margin"], size="aum")
        assert _extract_payload(block)["size"] == "aum"

    def test_unset_options_absent(self) -> None:
        payload = _extract_payload(render_chart(chart_type="bar", data=DUAL_DATA, x="q", y=["rev"]))
        for omitted in ("y2", "y2Axis", "size", "referenceLines", "referenceBands"):
            assert omitted not in payload

    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"chart_type": "pie", "y2": ["margin"]}, "y2 is not supported for pie"),
            ({"chart_type": "bar", "y2": ["margin"], "horizontal": True}, "y2 does not support horizontal"),
            ({"chart_type": "bar", "y2": ["margin"], "stacked": True}, "y2 does not support stacked"),
            ({"chart_type": "bar", "y2": ["rev"]}, "must not repeat y fields"),
            ({"chart_type": "bar", "y2": []}, "y2 must be a non-empty list"),
            ({"chart_type": "bar", "y2": ["missing"]}, "not present in the first data row"),
            ({"chart_type": "bar", "y2_axis": {"name": "%"}}, "y2_axis requires y2"),
            (
                {"chart_type": "bar", "y2": ["margin"], "y2_axis": {"log": True, "min": 0}},
                "y2_axis min must be greater than 0",
            ),
            ({"chart_type": "bar", "size": "aum"}, "size requires chart_type 'scatter'"),
            ({"chart_type": "scatter", "size": "missing"}, "not present in the first data row"),
            (
                {"chart_type": "pie", "reference_lines": [{"axis": "x", "value": "Q1"}]},
                "not supported for pie",
            ),
            (
                {"chart_type": "bar", "reference_lines": [{"axis": "y", "value": "high"}]},
                "reference line values on the y axis must be numeric",
            ),
            (
                {"chart_type": "bar", "reference_lines": [{"axis": "y", "value": True}]},
                "must be numeric",
            ),
            (
                {"chart_type": "bar", "y_axis": {"log": True}, "reference_bands": [{"axis": "y", "from": 0, "to": 5}]},
                "greater than 0 on a log axis",
            ),
            (
                {"chart_type": "bar", "reference_bands": [{"axis": "y", "from": 9, "to": 3}]},
                "from must be less than to",
            ),
            (
                {"chart_type": "bar", "reference_lines": [{"axis": "z", "value": 1}]},
                "axis must be 'x' or 'y'",
            ),
            (
                {"chart_type": "bar", "reference_lines": [{"axis": "y"}]},
                r"missing keys: \['value'\]",
            ),
            (
                {"chart_type": "bar", "reference_lines": [{"axis": "y", "value": 1, "color": "red"}]},
                "unsupported keys",
            ),
            (
                {
                    "chart_type": "bar",
                    "reference_lines": [{"axis": "y", "value": i} for i in range(MAX_REFERENCE_MARKS + 1)],
                },
                f"at most {MAX_REFERENCE_MARKS}",
            ),
        ],
    )
    def test_invalid_combinations_raise(self, kwargs: dict[str, Any], message: str) -> None:
        with pytest.raises(ValueError, match=message):
            render_chart(data=DUAL_DATA, x="q", y=["rev"], **kwargs)


FLOW_DATA = [
    {"src": "Budget", "dst": "Compute", "cost": 60},
    {"src": "Compute", "dst": "Jobs", "cost": 40},
]


class TestRenderChartSankey:
    def test_round_trips_target(self) -> None:
        payload = _extract_payload(render_chart(chart_type="sankey", data=FLOW_DATA, x="src", y=["cost"], target="dst"))
        assert payload["type"] == "sankey"
        assert payload["target"] == "dst"

    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"chart_type": "sankey"}, "sankey requires target"),
            ({"chart_type": "bar", "target": "dst"}, "target requires chart_type 'sankey'"),
            ({"chart_type": "sankey", "target": "src"}, "target must differ from x"),
            ({"chart_type": "sankey", "target": "dst", "x_name": "Source"}, "sankey does not support x_name"),
            ({"chart_type": "sankey", "target": "missing"}, "missing"),
        ],
    )
    def test_invalid_combinations_raise(self, kwargs: dict[str, Any], message: str) -> None:
        with pytest.raises(ValueError, match=message):
            render_chart(data=FLOW_DATA, x="src", y=["cost"], **kwargs)

    def test_rejects_a_second_y(self) -> None:
        with pytest.raises(ValueError, match="exactly one y"):
            render_chart(chart_type="sankey", data=FLOW_DATA, x="src", y=["cost", "cost"], target="dst")

    def test_rejects_loops_and_names_their_nodes(self) -> None:
        data = [*FLOW_DATA, {"src": "Jobs", "dst": "Budget", "cost": 1}]
        with pytest.raises(ValueError, match=r"loop back \(Budget, Compute, Jobs\)"):
            render_chart(chart_type="sankey", data=data, x="src", y=["cost"], target="dst")

    def test_caps_the_node_count(self) -> None:
        data = [{"src": "Root", "dst": f"Leaf {i}", "cost": 1} for i in range(MAX_SANKEY_NODES)]
        with pytest.raises(ValueError, match=f"{MAX_SANKEY_NODES + 1} nodes; at most {MAX_SANKEY_NODES}"):
            render_chart(chart_type="sankey", data=data, x="src", y=["cost"], target="dst")

    def test_ignores_rows_that_draw_nothing(self) -> None:
        data: list[dict[str, Any]] = [
            *FLOW_DATA,
            {"src": "Jobs", "dst": "Budget", "cost": 0},
            {"src": None, "dst": "Budget", "cost": 5},
        ]
        render_chart(chart_type="sankey", data=data, x="src", y=["cost"], target="dst")


class TestLoopingNodes:
    def test_keeps_only_nodes_on_a_loop(self) -> None:
        pairs = [("Start", "A"), ("A", "B"), ("B", "A"), ("B", "End")]
        assert looping_nodes(pairs) == ["A", "B"]

    def test_self_loops_and_clean_flows(self) -> None:
        assert looping_nodes([("A", "A")]) == ["A"]
        assert looping_nodes([("A", "B"), ("B", "C")]) == []
