"""Read more of a large SQL result (#688): ``_read_result``.

SQL tool results reach the model bounded (``sql_result_compact``).  To read
further rows, chosen columns or a full cell, the model passes the identical
query again; the tool reruns it through the managed SQL MCP server's
read-only tool, as the user, and returns only the requested slice.  The
warehouse answers an identical query from its result cache, so nothing is
stored here.  The tool is offered only when such a read-only tool is mounted.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from agents import FunctionTool
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from payloads.agent.runtime.mcp_wrapper import FilteredMCPServerStreamableHttp
from payloads.agent.runtime.sql_rerun import (
    MAX_QUERY_CHARS,
    RerunError,
    find_read_only_sql_tool,
    invalid_args_message,
    rerun_query,
)
from payloads.agent.runtime.sql_result_compact import (
    DEFAULT_ROW_CAP,
    MAX_RESULT_CHARS,
    bound_compact,
    serialize_compact,
)

READ_RESULT_TOOL_NAME: Final = "_read_result"

_GUIDANCE: Final = (
    "## Large SQL results\n"
    f"- A SQL result shows at most {DEFAULT_ROW_CAP} rows and about {MAX_RESULT_CHARS:,} characters. When it "
    "doesn't fit, rows are left out (`truncated`) and long cells are cut to end in `…[+N chars]`.\n"
    "- Use SQL to search, filter and profile (`WHERE`, `GROUP BY`, aggregates). Use "
    f"`{READ_RESULT_TOOL_NAME}` with the identical query for a few more rows, chosen columns, or a cell ending "
    "in `…[+N chars]` (`row` and `column`); read such a cell in full before relying on what it holds.\n"
    "- `total_rows_is_minimum` means the SQL tool stopped at that many rows; filter to reach the rest."
)
_DESCRIPTION: Final = (
    "Read more of a SQL result you already got: rows from `offset` (up to `limit`), only some `columns`, or one "
    "full cell cut to `…[+N chars]` (`row` and `column`, paged by `char_offset`). Reads by position only; "
    "search and filter with a new SQL query instead. Pass the identical query text, so the warehouse answers "
    "from its cache. Runs read-only, as the user."
)
_PARAMS: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "minLength": 1, "maxLength": MAX_QUERY_CHARS},
        "offset": {"type": "integer", "minimum": 0, "description": "First row to return (0-based)."},
        "limit": {"type": "integer", "minimum": 1, "maximum": DEFAULT_ROW_CAP, "description": "Rows to return."},
        "columns": {"type": "array", "items": {"type": "string"}, "minItems": 1, "description": "Columns to keep."},
        "row": {"type": "integer", "minimum": 0, "description": "With `column`: the row of the cell to read."},
        "column": {"type": "string", "description": "With `row`: the column of the cell to read."},
        "char_offset": {"type": "integer", "minimum": 0, "description": "Where in the cell to start reading."},
    },
    "required": ["query"],
    "additionalProperties": False,
}
_USAGE: Final = (
    f"Give the query (1 to {MAX_QUERY_CHARS:,} characters), and either rows (`offset`, `limit` up to "
    f"{DEFAULT_ROW_CAP}, `columns`) or one cell (`row` and `column`, optional `char_offset`)."
)


class _ReadArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=MAX_QUERY_CHARS)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=DEFAULT_ROW_CAP, ge=1, le=DEFAULT_ROW_CAP)
    columns: list[str] | None = Field(default=None, min_length=1)
    row: int | None = Field(default=None, ge=0)
    column: str | None = None
    char_offset: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _cell_needs_row_and_column(self) -> _ReadArgs:
        if (self.row is None) != (self.column is None):
            raise ValueError("row and column go together")
        return self


class _ReadError(Exception):
    """A message for the model about a request the result can't answer."""


def _column_indexes(columns: list[str], wanted: list[str]) -> list[int]:
    unknown = [name for name in wanted if name not in columns]
    if unknown:
        raise _ReadError(f"Error: no column {', '.join(unknown)}; the result has {', '.join(columns)}.")
    return [columns.index(name) for name in wanted]


def _row_count_note(result: dict[str, Any], rows: int) -> str:
    if result.get("total_rows_is_minimum"):
        return f"{rows} rows (the SQL tool returns at most these; filter to reach the rest)"
    return f"{rows} rows"


def _render_rows(result: dict[str, Any], args: _ReadArgs) -> str:
    columns: list[str] = result["columns"]
    rows: list[list[Any]] = result["rows"]
    if args.offset >= len(rows):
        note = _row_count_note(result, len(rows))
        raise _ReadError(f"Error: no rows from offset {args.offset}; the result has {note}.")
    indexes = _column_indexes(columns, args.columns or columns)
    selected = [[row[i] for i in indexes] for row in rows[args.offset : args.offset + args.limit]]
    page = bound_compact(
        {"columns": [columns[i] for i in indexes], "rows": selected, "total_rows": len(selected)}, args.limit
    )
    end = args.offset + len(page["rows"])
    out: dict[str, Any] = {
        "columns": page["columns"],
        "rows": page["rows"],
        "offset": args.offset,
        "total_rows": result.get("total_rows", len(rows)),
    }
    if result.get("total_rows_is_minimum"):
        out["total_rows_is_minimum"] = True
    if end < len(rows):
        out["next_offset"] = end
    return serialize_compact(out)


def _cell_text(cell: Any) -> str:
    if cell is None:
        return "NULL"
    if isinstance(cell, str):
        return cell
    return json.dumps(cell, ensure_ascii=False)


def _render_cell(result: dict[str, Any], row: int, column: str, char_offset: int) -> str:
    rows: list[list[Any]] = result["rows"]
    if row >= len(rows):
        raise _ReadError(f"Error: no row {row}; the result has {_row_count_note(result, len(rows))}.")
    text = _cell_text(rows[row][_column_indexes(result["columns"], [column])[0]])
    start = min(char_offset, len(text))
    end = min(start + MAX_RESULT_CHARS, len(text))
    header = f"Row {row}, column {column}, characters {start}-{end} of {len(text)}:"
    if end < len(text):
        footer = f"More: `{READ_RESULT_TOOL_NAME}` with row={row}, column={column!r}, char_offset={end}."
    else:
        footer = "End of cell."
    return f"{header}\n{text[start:end]}\n\n{footer}"


def _render(result: dict[str, Any], args: _ReadArgs) -> str:
    if args.row is not None and args.column is not None:
        return _render_cell(result, args.row, args.column, args.char_offset)
    return _render_rows(result, args)


@dataclass(frozen=True)
class ReadResult:
    """Reruns the query through the read-only SQL tool and returns the requested slice."""

    server: FilteredMCPServerStreamableHttp
    tool_name: str

    async def __call__(self, ctx: Any, args_json: str) -> str:
        try:
            args = _ReadArgs.model_validate_json(args_json or "{}")
        except ValidationError as exc:
            return invalid_args_message(exc, _USAGE)
        try:
            result = await rerun_query(self.server, self.tool_name, args.query, ctx)
            return _render(result, args)
        except (RerunError, _ReadError) as exc:
            return str(exc)


@dataclass(frozen=True)
class AgentResultReader:
    """This request's ``_read_result`` tool and its guidance."""

    tools: tuple[FunctionTool, ...]
    guidance: str


def prepare_result_reader(mcp_servers: Sequence[object]) -> AgentResultReader | None:
    """The tool and guidance when a managed SQL server with the read-only tool is mounted."""
    found = find_read_only_sql_tool(mcp_servers)
    if found is None:
        return None
    server, tool_name = found
    tool = FunctionTool(
        name=READ_RESULT_TOOL_NAME,
        description=_DESCRIPTION,
        params_json_schema=_PARAMS,
        on_invoke_tool=ReadResult(server, tool_name),
        strict_json_schema=False,
    )
    return AgentResultReader(tools=(tool,), guidance=_GUIDANCE)
