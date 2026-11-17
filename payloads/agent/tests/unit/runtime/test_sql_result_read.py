"""Tests for ``payloads.agent.runtime.sql_result_read`` (#688)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_foundry.contracts.mcp import ToolAllowEntry
from payloads.agent.runtime.mcp_wrapper import FilteredMCPServerStreamableHttp
from payloads.agent.runtime.sql_rerun import READ_ONLY_SQL_TOOL
from payloads.agent.runtime.sql_result_compact import DEFAULT_ROW_CAP, MAX_CELL_CHARS, MAX_RESULT_CHARS
from payloads.agent.runtime.sql_result_read import (
    READ_RESULT_TOOL_NAME,
    ReadResult,
    prepare_result_reader,
)

_SQL_URL = "https://example.invalid/api/2.0/mcp/sql"
_QUERY = "SELECT id, note FROM shared_catalog.sales.orders ORDER BY id"


def _server(url: str = _SQL_URL, allow_entries: list[ToolAllowEntry] | None = None) -> FilteredMCPServerStreamableHttp:
    return FilteredMCPServerStreamableHttp(
        params={"url": url},
        name_prefix="sql__",
        allow_entries=allow_entries,
        server_url=url,
        cache_tools_list=False,
        name="sql",
    )


def _envelope(rows: list[list[Any]], *, truncated: bool = False) -> SimpleNamespace:
    body = {
        "status": {"state": "SUCCEEDED"},
        "manifest": {"schema": {"columns": [{"name": "id"}, {"name": "note"}]}, "total_row_count": len(rows)},
        "result": {"data_array": rows},
        "truncated": truncated,
    }
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(body))], isError=False)


def _reader(response: Any) -> tuple[ReadResult, AsyncMock]:
    server = _server()
    call = AsyncMock(return_value=response)
    server.call_tool_unbounded = call  # type: ignore[method-assign]
    return ReadResult(server, "sql__execute_sql_read_only"), call


async def _read(response: Any, **args: Any) -> str:
    reader, _ = _reader(response)
    return await reader(MagicMock(), json.dumps({"query": _QUERY, **args}))


_ROWS = [[i, f"note {i}"] for i in range(120)]


class TestPrepareResultReader:
    def test_offered_for_the_managed_sql_server(self) -> None:
        reader = prepare_result_reader([_server()])
        assert reader is not None
        assert [tool.name for tool in reader.tools] == [READ_RESULT_TOOL_NAME]
        assert READ_RESULT_TOOL_NAME in reader.guidance

    def test_not_offered_for_other_servers(self) -> None:
        assert prepare_result_reader([_server(url="https://example.invalid/api/2.0/mcp/genie/space")]) is None
        assert prepare_result_reader([object()]) is None
        assert prepare_result_reader([]) is None

    def test_not_offered_when_the_allow_list_drops_the_read_only_tool(self) -> None:
        server = _server(allow_entries=[ToolAllowEntry(match="execute_sql")])
        assert prepare_result_reader([server]) is None

    @pytest.mark.asyncio
    async def test_reruns_through_the_read_only_tool_with_the_identical_query(self) -> None:
        server = _server(allow_entries=[ToolAllowEntry(match=READ_ONLY_SQL_TOOL, expose_as="query")])
        server.call_tool_unbounded = AsyncMock(return_value=_envelope(_ROWS))  # type: ignore[method-assign]
        reader = prepare_result_reader([server])
        assert reader is not None
        ctx = MagicMock()

        await reader.tools[0].on_invoke_tool(ctx, json.dumps({"query": _QUERY}))

        server.call_tool_unbounded.assert_awaited_once_with("sql__query", {"query": _QUERY}, ctx)


class TestRows:
    @pytest.mark.asyncio
    async def test_reads_rows_from_an_offset(self) -> None:
        page = json.loads(await _read(_envelope(_ROWS), offset=50, limit=10))
        assert page["columns"] == ["id", "note"]
        assert page["rows"] == [[i, f"note {i}"] for i in range(50, 60)]
        assert page["offset"] == 50
        assert page["next_offset"] == 60
        assert page["total_rows"] == 120

    @pytest.mark.asyncio
    async def test_last_page_has_no_next_offset(self) -> None:
        page = json.loads(await _read(_envelope(_ROWS), offset=110))
        assert len(page["rows"]) == 10
        assert "next_offset" not in page

    @pytest.mark.asyncio
    async def test_keeps_only_the_requested_columns(self) -> None:
        page = json.loads(await _read(_envelope(_ROWS), columns=["note"], limit=2))
        assert page["columns"] == ["note"]
        assert page["rows"] == [["note 0"], ["note 1"]]

    @pytest.mark.asyncio
    async def test_page_is_bounded(self) -> None:
        wide = [[i, "n" * 1_000] for i in range(DEFAULT_ROW_CAP)]
        text = await _read(_envelope(wide))
        page = json.loads(text)
        assert len(text) <= MAX_RESULT_CHARS + 100
        assert all(len(row[1]) <= MAX_CELL_CHARS for row in page["rows"])
        assert page["next_offset"] == len(page["rows"])

    @pytest.mark.asyncio
    async def test_unknown_column_lists_the_columns(self) -> None:
        assert await _read(_envelope(_ROWS), columns=["price"]) == "Error: no column price; the result has id, note."

    @pytest.mark.asyncio
    async def test_offset_past_the_end(self) -> None:
        assert await _read(_envelope(_ROWS), offset=500) == "Error: no rows from offset 500; the result has 120 rows."

    @pytest.mark.asyncio
    async def test_marks_a_total_the_sql_tool_capped(self) -> None:
        page = json.loads(await _read(_envelope(_ROWS, truncated=True), offset=0, limit=1))
        assert page["total_rows_is_minimum"] is True


class TestCell:
    @pytest.mark.asyncio
    async def test_reads_a_full_cell_in_pages(self) -> None:
        long_note = "".join(str(i % 10) for i in range(MAX_RESULT_CHARS + 500))
        response = _envelope([[1, long_note]])

        first = await _read(response, row=0, column="note")
        rest = await _read(response, row=0, column="note", char_offset=MAX_RESULT_CHARS)

        assert first.startswith(f"Row 0, column note, characters 0-{MAX_RESULT_CHARS} of {len(long_note)}:\n0123")
        assert first.endswith(f"char_offset={MAX_RESULT_CHARS}.")
        assert rest.endswith("End of cell.")
        assert long_note[MAX_RESULT_CHARS:] in rest

    @pytest.mark.asyncio
    async def test_row_past_the_end(self) -> None:
        assert await _read(_envelope(_ROWS), row=500, column="note") == ("Error: no row 500; the result has 120 rows.")


class TestFailures:
    @pytest.mark.asyncio
    async def test_invalid_arguments(self) -> None:
        reader, call = _reader(_envelope(_ROWS))
        out = await reader(MagicMock(), json.dumps({"query": _QUERY, "row": 1}))
        assert out.startswith("Error: row and column go together. Give the query")
        call.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_invalid_arguments_name_the_field_without_echoing_input(self) -> None:
        reader, _ = _reader(_envelope(_ROWS))
        out = await reader(MagicMock(), json.dumps({"query": _QUERY, "limit": DEFAULT_ROW_CAP + 1, "offset": -5}))
        assert "`limit`: Input should be less than or equal to" in out
        assert "`offset`: Input should be greater than or equal to 0" in out
        assert _QUERY not in out

    @pytest.mark.asyncio
    async def test_query_error_is_passed_on_shortened(self) -> None:
        error = SimpleNamespace(content=[SimpleNamespace(type="text", text="x" * 2_000)], isError=True)
        out = await _read(error)
        assert out.startswith("Error: the query returned no result to read: xxx")
        assert len(out) < 600

    @pytest.mark.asyncio
    async def test_transport_failure(self) -> None:
        reader, call = _reader(None)
        call.side_effect = RuntimeError("connection reset")
        out = await reader(MagicMock(), json.dumps({"query": _QUERY}))
        assert out == "Error: the SQL tool is unavailable right now; continue without it."
