"""Tests for ``payloads.agent.runtime.sql_result_compact``."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from payloads.agent.runtime.sql_result_compact import (
    DEFAULT_ROW_CAP,
    MAX_CELL_CHARS,
    MAX_RESULT_CHARS,
    compact_call_tool_result,
    compact_sql_tool_text,
    is_sql_execute_upstream,
)


def _envelope(
    *,
    columns: list[str],
    rows: list[list[Any]],
    state: str = "SUCCEEDED",
    statement_id: str = "01-test",
    total_row_count: int | None = None,
    wrap_cells: bool = False,
) -> dict[str, Any]:
    data_rows: list[Any]
    if wrap_cells:
        data_rows = [{"values": [{"string_value": str(c)} for c in row]} for row in rows]
    else:
        data_rows = rows
    body: dict[str, Any] = {
        "statement_id": statement_id,
        "status": {"state": state},
        "manifest": {
            "format": "JSON_ARRAY",
            "schema": {
                "column_count": len(columns),
                "columns": [
                    {
                        "name": name,
                        "type_text": "STRING",
                        "type_name": "STRING",
                        "position": i,
                    }
                    for i, name in enumerate(columns)
                ],
            },
        },
        "result": {
            "chunk_index": 0,
            "row_count": len(rows),
            "data_array": data_rows,
        },
    }
    if total_row_count is not None:
        body["manifest"]["total_row_count"] = total_row_count
    return body


class TestIsSqlExecuteUpstream:
    def test_known_names(self) -> None:
        assert is_sql_execute_upstream("execute_sql")
        assert is_sql_execute_upstream("execute_sql_read_only")

    def test_rejects_others(self) -> None:
        assert not is_sql_execute_upstream("poll_sql_result")
        assert not is_sql_execute_upstream("sql_warehouse__execute_sql_read_only")


class TestCompactSqlToolText:
    def test_bare_envelope(self) -> None:
        raw = json.dumps(
            _envelope(columns=["n"], rows=[["11"]]),
        )
        out = compact_sql_tool_text(raw)
        assert out is not None
        parsed = json.loads(out)
        assert parsed == {
            "columns": ["n"],
            "rows": [["11"]],
            "total_rows": 1,
            "truncated": False,
            "statement_id": "01-test",
        }
        # Compact separators -- no spaces after ``:`` / ``,``.
        assert " :" not in out
        assert ", " not in out

    def test_query_attachments_wrapper(self) -> None:
        inner = _envelope(
            columns=["table_catalog", "table_count"],
            rows=[["main", 3]],
        )
        wrapped = {
            "content": {
                "queryAttachments": [
                    {
                        "query": "SELECT 1",
                        "description": "verbose restatement of the query",
                        "statement_response": inner,
                    }
                ]
            }
        }
        out = compact_sql_tool_text(json.dumps(wrapped))
        assert out is not None
        parsed = json.loads(out)
        assert parsed["columns"] == ["table_catalog", "table_count"]
        assert parsed["rows"] == [["main", 3]]
        assert "description" not in parsed
        assert "manifest" not in parsed

    def test_unwraps_value_wrapped_cells(self) -> None:
        raw = json.dumps(
            _envelope(columns=["a", "b"], rows=[["x", "1"]], wrap_cells=True),
        )
        parsed = json.loads(compact_sql_tool_text(raw) or "")
        assert parsed["rows"] == [["x", "1"]]

    def test_unwraps_null_value_wrapper(self) -> None:
        # JSON_ARRAY null cells arrive as ``{"null_value": "NULL_VALUE"}``;
        # leaving that dict in place is ~27 chars of waste per null.
        body = _envelope(columns=["name", "note"], rows=[["demo2", "x"]])
        body["result"]["data_array"] = [
            [
                {"string_value": "demo2"},
                {"null_value": "NULL_VALUE"},
            ]
        ]
        parsed = json.loads(compact_sql_tool_text(json.dumps(body)) or "")
        assert parsed["rows"] == [["demo2", None]]
        assert "null_value" not in json.dumps(parsed)
        assert "NULL_VALUE" not in json.dumps(parsed)

    def test_empty_typed_cell_is_null(self) -> None:
        # The managed SQL MCP's PROTOBUF_ARRAY sends NULL as an empty object.
        body = _envelope(columns=["name", "note"], rows=[])
        body["result"] = {
            "data_typed_array": [{"values": [{"str": "demo2"}, {}]}],
        }
        parsed = json.loads(compact_sql_tool_text(json.dumps(body)) or "")
        assert parsed["rows"] == [["demo2", None]]

    def test_row_cap_sets_truncated(self) -> None:
        rows = [[str(i)] for i in range(5)]
        raw = json.dumps(
            _envelope(columns=["i"], rows=rows, total_row_count=5),
        )
        parsed = json.loads(compact_sql_tool_text(raw, row_cap=2) or "")
        assert parsed["rows"] == [["0"], ["1"]]
        assert parsed["truncated"] is True
        assert parsed["omitted_rows"] == 3
        assert parsed["total_rows"] == 5

    def test_default_row_cap(self) -> None:
        rows = [[str(i)] for i in range(DEFAULT_ROW_CAP + 10)]
        raw = json.dumps(_envelope(columns=["i"], rows=rows))
        parsed = json.loads(compact_sql_tool_text(raw) or "")
        assert len(parsed["rows"]) == DEFAULT_ROW_CAP
        assert parsed["truncated"] is True
        assert parsed["omitted_rows"] == 10

    def test_pending_left_alone(self) -> None:
        raw = json.dumps(
            _envelope(columns=["n"], rows=[], state="PENDING"),
        )
        assert compact_sql_tool_text(raw) is None

    def test_failed_left_alone(self) -> None:
        raw = json.dumps(
            _envelope(columns=["n"], rows=[], state="FAILED"),
        )
        assert compact_sql_tool_text(raw) is None

    def test_non_json_left_alone(self) -> None:
        assert compact_sql_tool_text("not-json") is None

    def test_already_compact_is_idempotent(self) -> None:
        compact = {
            "columns": ["n"],
            "rows": [["1"]],
            "total_rows": 1,
            "truncated": False,
            "statement_id": "x",
        }
        out = compact_sql_tool_text(json.dumps(compact))
        assert out is not None
        assert json.loads(out) == compact

    def test_drops_type_metadata(self) -> None:
        raw = json.dumps(_envelope(columns=["c"], rows=[["v"]]))
        out = compact_sql_tool_text(raw) or ""
        assert "type_text" not in out
        assert "type_name" not in out
        assert "position" not in out
        assert "JSON_ARRAY" not in out

    def test_row_cap_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="row_cap"):
            compact_sql_tool_text("{}", row_cap=0)


class TestBounds:
    def test_long_cells_stay_whole_when_the_result_fits(self) -> None:
        # A DESCRIBE with one long struct type must not lose the end of the type.
        long_type = "struct<" + ",".join(f"field_{i}:string" for i in range(40)) + ">"
        rows = [["trace_id", "string"], ["assessments", long_type]]
        raw = json.dumps(_envelope(columns=["col_name", "data_type"], rows=rows))
        assert json.loads(compact_sql_tool_text(raw) or "")["rows"] == rows

    def test_long_text_cells_are_shortened_when_the_result_does_not_fit(self) -> None:
        long_text = "x" * 1_000
        raw = json.dumps(_envelope(columns=["note"], rows=[[long_text]] * 20))
        parsed = json.loads(compact_sql_tool_text(raw) or "")
        cell = parsed["rows"][0][0]
        assert len(parsed["rows"]) == 20
        assert len(cell) <= MAX_CELL_CHARS
        omitted = 1_000 - cell.index("…")
        assert cell.endswith(f"…[+{omitted} chars]")

    def test_large_nested_cells_are_shortened_as_json_when_the_result_does_not_fit(self) -> None:
        nested = {"items": list(range(2_000))}
        raw = json.dumps(_envelope(columns=["payload"], rows=[[nested]]))
        cell = json.loads(compact_sql_tool_text(raw) or "")["rows"][0][0]
        assert isinstance(cell, str)
        assert cell.startswith('{"items":[0,1,2')
        assert len(cell) <= MAX_CELL_CHARS

    def test_small_nested_cell_is_unchanged(self) -> None:
        raw = json.dumps(_envelope(columns=["payload"], rows=[[{"a": 1}]]))
        assert json.loads(compact_sql_tool_text(raw) or "")["rows"] == [[{"a": 1}]]

    def test_small_result_is_unchanged_byte_for_byte(self) -> None:
        compact = '{"columns":["n"],"rows":[["1"],["2"]],"total_rows":2,"truncated":false}'
        assert compact_sql_tool_text(compact) == compact

    def test_wide_rows_fit_the_size_budget(self) -> None:
        columns = [f"c{i}" for i in range(10)]
        rows = [["y" * 150 for _ in columns] for _ in range(DEFAULT_ROW_CAP)]
        out = compact_sql_tool_text(json.dumps(_envelope(columns=columns, rows=rows))) or ""
        parsed = json.loads(out)
        assert len(out) <= MAX_RESULT_CHARS + 100
        assert 1 <= len(parsed["rows"]) < DEFAULT_ROW_CAP
        assert parsed["truncated"] is True
        assert parsed["omitted_rows"] == DEFAULT_ROW_CAP - len(parsed["rows"])

    def test_one_row_is_kept_when_it_alone_exceeds_the_budget(self) -> None:
        columns = [f"c{i}" for i in range(100)]
        rows = [["z" * 150 for _ in columns] for _ in range(3)]
        parsed = json.loads(compact_sql_tool_text(json.dumps(_envelope(columns=columns, rows=rows))) or "")
        assert len(parsed["rows"]) == 1
        assert parsed["omitted_rows"] == 2

    def test_bounding_is_idempotent(self) -> None:
        columns = [f"c{i}" for i in range(10)]
        rows = [["w" * 500 for _ in columns] for _ in range(DEFAULT_ROW_CAP)]
        once = compact_sql_tool_text(json.dumps(_envelope(columns=columns, rows=rows))) or ""
        assert compact_sql_tool_text(once) == once

    def test_upstream_truncation_marks_total_as_minimum(self) -> None:
        body = _envelope(columns=["i"], rows=[[str(i)] for i in range(80)], total_row_count=20_480)
        body["truncated"] = True
        parsed = json.loads(compact_sql_tool_text(json.dumps(body)) or "")
        assert parsed["truncated"] is True
        assert parsed["total_rows_is_minimum"] is True
        assert parsed["total_rows"] == 20_480
        assert len(parsed["rows"]) == DEFAULT_ROW_CAP
        assert "omitted_rows" not in parsed

    def test_manifest_truncation_is_carried_over_for_small_results(self) -> None:
        body = _envelope(columns=["i"], rows=[["1"]], total_row_count=1)
        body["manifest"]["truncated"] = True
        parsed = json.loads(compact_sql_tool_text(json.dumps(body)) or "")
        assert parsed["truncated"] is True
        assert parsed["total_rows_is_minimum"] is True


class TestCompactCallToolResult:
    def test_mutates_text_content(self) -> None:
        payload = _envelope(columns=["n"], rows=[["7"]])
        item = SimpleNamespace(type="text", text=json.dumps(payload))
        result = SimpleNamespace(content=[item])

        assert compact_call_tool_result(result) is True
        parsed = json.loads(item.text)
        assert parsed["columns"] == ["n"]
        assert parsed["rows"] == [["7"]]

    def test_returns_false_when_not_sql(self) -> None:
        item = SimpleNamespace(type="text", text='{"ok": true}')
        result = SimpleNamespace(content=[item])
        assert compact_call_tool_result(result) is False
        assert item.text == '{"ok": true}'
