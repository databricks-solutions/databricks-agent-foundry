"""Tests for ``payloads.agent.runtime.chart_helpers``."""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from payloads.agent.runtime.chart_helpers import (
    MAX_DATA_ROWS,
    SUPPORTED_CHART_TYPES,
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
    @pytest.mark.parametrize("chart_type", sorted(SUPPORTED_CHART_TYPES))
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
