"""Tests for ``payloads.agent.runtime.tool_output_renderers``.

Covers the SQL warehouse Statement Execution API renderer
(default registration) and the regex registry semantics.
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any

import pytest

from payloads.agent.runtime import tool_output_renderers as tor
from payloads.agent.runtime.tool_output_renderers import (
    register,
    render_extra_text_part,
    render_sql_warehouse_output,
)

SQL_TOOL_NAME = "sql_warehouse__execute_sql_abc123"
SAMPLE_QUERY = "SELECT count(*) AS n FROM main.default.events"


def _success_envelope(columns: list[str] | None = None, rows: list[list[object]] | None = None) -> dict[str, Any]:
    """Minimal Statement Execution API success envelope."""
    columns = columns or ["n"]
    rows = rows if rows is not None else [["11"]]
    return {
        "statement_id": "01-test",
        "status": {"state": "SUCCEEDED"},
        "manifest": {
            "format": "JSON_ARRAY",
            "schema": {
                "column_count": len(columns),
                "columns": [{"name": c, "type_name": "STRING"} for c in columns],
            },
        },
        "result": {
            "chunk_index": 0,
            "row_offset": 0,
            "row_count": len(rows),
            "data_array": rows,
        },
    }


def _decode_table_payload(text: str) -> dict[str, Any]:
    """Pull the base64 payload out of a ``table-download`` fence header."""
    match = re.search(r"```table-download:result\.csv:([^\n]+)\n", text)
    assert match, f"no table-download fence in:\n{text}"
    payload: dict[str, Any] = json.loads(base64.b64decode(match.group(1)).decode("utf-8"))
    return payload


def _decode_sql_payload(text: str) -> str:
    match = re.search(r"```sql-download:query\.sql:([^\n]+)\n", text)
    assert match, f"no sql-download fence in:\n{text}"
    return base64.b64decode(match.group(1)).decode("utf-8")


class TestSqlWarehouseRenderer:
    def test_happy_path_emits_sql_and_table_fences(self) -> None:
        envelope = _success_envelope()
        arguments = json.dumps({"query": SAMPLE_QUERY})

        rendered = render_sql_warehouse_output(
            SQL_TOOL_NAME,
            arguments,
            envelope,
        )

        assert rendered is not None
        assert _decode_sql_payload(rendered) == SAMPLE_QUERY
        payload = _decode_table_payload(rendered)
        assert payload == {
            "columns": ["n"],
            "rows": [["11"]],
            "totalRows": 1,
        }

    def test_output_has_trailing_blank_line(self) -> None:
        # The UI concatenates consecutive ``text`` parts as raw strings,
        # so we must ensure the next part doesn't fuse against our
        # closing ``` and break Streamdown's fence parser.
        rendered = render_sql_warehouse_output(
            SQL_TOOL_NAME,
            {"query": SAMPLE_QUERY},
            _success_envelope(),
        )
        assert rendered is not None
        assert rendered.endswith("\n\n"), f"renderer must end with a paragraph break; got {rendered[-10:]!r}"

    def test_accepts_dict_arguments(self) -> None:
        rendered = render_sql_warehouse_output(
            SQL_TOOL_NAME,
            {"query": SAMPLE_QUERY},
            _success_envelope(),
        )
        assert rendered is not None
        assert _decode_sql_payload(rendered) == SAMPLE_QUERY

    def test_accepts_json_string_envelope(self) -> None:
        envelope = _success_envelope()
        rendered = render_sql_warehouse_output(
            SQL_TOOL_NAME,
            {"query": SAMPLE_QUERY},
            json.dumps(envelope),
        )
        assert rendered is not None
        assert _decode_table_payload(rendered)["columns"] == ["n"]

    def test_accepts_mcp_content_block_list(self) -> None:
        # The OpenAI Agents SDK forwards MCP tool output as a list of
        # ``{"text": "<json>"}`` content blocks when result.content has
        # more than one item.
        envelope = _success_envelope()
        mcp_output = [{"text": json.dumps(envelope), "type": "text"}]
        rendered = render_sql_warehouse_output(
            SQL_TOOL_NAME,
            {"query": SAMPLE_QUERY},
            mcp_output,
        )
        assert rendered is not None
        assert _decode_table_payload(rendered)["columns"] == ["n"]

    def test_accepts_mcp_single_text_block(self) -> None:
        # The SDK collapses a single-item result.content to one
        # ``{"type": "text", "text": "<json>"}`` dict (NOT a list).
        # This is the common case for SQL warehouse tools.
        envelope = _success_envelope()
        single_block = {"type": "text", "text": json.dumps(envelope)}
        rendered = render_sql_warehouse_output(
            SQL_TOOL_NAME,
            {"query": SAMPLE_QUERY},
            single_block,
        )
        assert rendered is not None
        assert _decode_table_payload(rendered)["columns"] == ["n"]

    def test_accepts_responses_input_text_block(self) -> None:
        # The Responses API uses ``input_text`` rather than the MCP-native
        # ``text`` discriminator; the SDK emits this shape when forwarding
        # MCP tool returns into the chat transcript.
        envelope = _success_envelope()
        block = {"type": "input_text", "text": json.dumps(envelope)}
        rendered_single = render_sql_warehouse_output(
            SQL_TOOL_NAME,
            {"query": SAMPLE_QUERY},
            block,
        )
        rendered_list = render_sql_warehouse_output(
            SQL_TOOL_NAME,
            {"query": SAMPLE_QUERY},
            [block],
        )
        assert rendered_single is not None
        assert rendered_list is not None
        assert _decode_table_payload(rendered_single) == _decode_table_payload(rendered_list)

    def test_skips_mcp_single_text_block_with_non_json(self) -> None:
        rendered = render_sql_warehouse_output(
            SQL_TOOL_NAME,
            {"query": SAMPLE_QUERY},
            {"type": "text", "text": "not json"},
        )
        assert rendered is None

    def test_skips_mcp_content_blocks_without_text(self) -> None:
        rendered = render_sql_warehouse_output(
            SQL_TOOL_NAME,
            {"query": SAMPLE_QUERY},
            [{"type": "image", "url": "..."}],
        )
        assert rendered is None

    def test_skips_mcp_content_blocks_with_non_json_text(self) -> None:
        rendered = render_sql_warehouse_output(
            SQL_TOOL_NAME,
            {"query": SAMPLE_QUERY},
            [{"text": "not json"}],
        )
        assert rendered is None

    def test_empty_result_still_emits_table(self) -> None:
        rendered = render_sql_warehouse_output(
            SQL_TOOL_NAME,
            {"query": SAMPLE_QUERY},
            _success_envelope(rows=[]),
        )
        assert rendered is not None
        payload = _decode_table_payload(rendered)
        assert payload["rows"] == []
        assert payload["totalRows"] == 0

    def test_failed_statement_skipped(self) -> None:
        envelope = _success_envelope()
        envelope["status"]["state"] = "FAILED"
        rendered = render_sql_warehouse_output(
            SQL_TOOL_NAME,
            {"query": SAMPLE_QUERY},
            envelope,
        )
        assert rendered is None

    def test_non_tabular_no_columns_skipped(self) -> None:
        envelope = _success_envelope()
        envelope["manifest"]["schema"]["columns"] = []
        rendered = render_sql_warehouse_output(
            SQL_TOOL_NAME,
            {"query": SAMPLE_QUERY},
            envelope,
        )
        assert rendered is None

    def test_unparseable_envelope_skipped(self) -> None:
        rendered = render_sql_warehouse_output(
            SQL_TOOL_NAME,
            {"query": SAMPLE_QUERY},
            "not json",
        )
        assert rendered is None

    def test_missing_query_skips_sql_fence_keeps_table(self) -> None:
        rendered = render_sql_warehouse_output(
            SQL_TOOL_NAME,
            {},
            _success_envelope(),
        )
        assert rendered is not None
        assert "sql-download" not in rendered
        assert "table-download" in rendered

    def test_query_special_characters_round_trip(self) -> None:
        query = "SELECT '\u00e9' AS x WHERE col = 'a\nb'"
        rendered = render_sql_warehouse_output(
            SQL_TOOL_NAME,
            {"query": query},
            _success_envelope(),
        )
        assert rendered is not None
        assert _decode_sql_payload(rendered) == query


class TestRegistryDispatch:
    def test_registry_empty_by_default(self) -> None:
        # SQL warehouse rendering is now handled client-side by the
        # tool.tsx override; the Python registry ships empty so callers
        # don't get duplicate output.  Future renderers (Genie, UC
        # functions) opt in via ``register(...)``.
        assert tor._REGISTRATIONS == []
        assert (
            render_extra_text_part(
                SQL_TOOL_NAME,
                {"query": SAMPLE_QUERY},
                _success_envelope(),
            )
            is None
        )

    def test_explicit_sql_warehouse_registration(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The SQL renderer is still importable + registerable for callers
        # that want fence-emission instead of (or in addition to) the
        # client-side Tool panel rendering.
        monkeypatch.setattr(tor, "_REGISTRATIONS", [])
        register(re.compile(r"sql_warehouse__execute_sql"), tor.render_sql_warehouse_output)
        rendered = render_extra_text_part(
            SQL_TOOL_NAME,
            {"query": SAMPLE_QUERY},
            _success_envelope(),
        )
        assert rendered is not None
        assert "sql-download" in rendered

    def test_unrelated_tool_returns_none(self) -> None:
        assert render_extra_text_part("genie__ask", {"q": "hi"}, "anything") is None

    def test_renderer_exception_swallowed(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        def boom(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("boom")

        monkeypatch.setattr(
            tor,
            "_REGISTRATIONS",
            [
                tor._Registration(re.compile(r"explode"), boom),
            ],
        )
        with caplog.at_level("WARNING"):
            assert render_extra_text_part("explode_tool", None, None) is None
        assert any("boom" in r.message for r in caplog.records)

    def test_register_appends_in_order(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[str] = []

        def first(name: str, _a: Any, _o: Any) -> str | None:
            calls.append(f"first:{name}")
            return None

        def second(name: str, _a: Any, _o: Any) -> str | None:
            calls.append(f"second:{name}")
            return "rendered"

        monkeypatch.setattr(tor, "_REGISTRATIONS", [])
        register(re.compile(r"^foo"), first)
        register(re.compile(r"^foo"), second)

        assert render_extra_text_part("foo_tool", None, None) == "rendered"
        assert calls == ["first:foo_tool", "second:foo_tool"]


@pytest.mark.parametrize(
    ("rows", "expected_total"),
    [
        ([], 0),
        ([["a"]], 1),
        ([["a"], ["b"], ["c"]], 3),
    ],
)
def test_total_rows_matches_data_array_length(rows: list[list[object]], expected_total: int) -> None:
    rendered = render_sql_warehouse_output(
        SQL_TOOL_NAME,
        {"query": SAMPLE_QUERY},
        _success_envelope(columns=["x"], rows=rows),
    )
    assert rendered is not None
    assert _decode_table_payload(rendered)["totalRows"] == expected_total
