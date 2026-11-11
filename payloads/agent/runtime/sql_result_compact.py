"""Compact SQL warehouse tool results for the agent transcript.

Databricks managed SQL MCP tools return a Statement Execution API
envelope (sometimes nested under ``queryAttachments``) with per-column
type metadata, chunk headers, and optionally value-wrapped cells.  The
chat UI and the model only need column names plus bare scalar rows.
The result is bounded: rows are kept up to ``DEFAULT_ROW_CAP`` and
``MAX_RESULT_CHARS``; long cells are shortened (``MAX_CELL_CHARS``) only
when whole rows don't fit.

This module is pure: given a JSON text payload it either returns a
compact JSON string or ``None`` (leave the upstream bytes unchanged).
The MCP wrapper calls it after auto-poll so every downstream consumer
(LLM loop, stream UI, Lakebase) sees the same shape.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Final

logger = logging.getLogger(__name__)

# Aligned with the sql_runner skill's default LIMIT guidance.
DEFAULT_ROW_CAP: Final[int] = 50

# When whole rows don't fit, longer cells are shortened to at most this many characters, marker included.
MAX_CELL_CHARS: Final[int] = 200

# Rows past this serialized size are dropped; at least one row is always kept.
MAX_RESULT_CHARS: Final[int] = 8_000

# Room for the ``…[+N chars]`` marker, so a shortened cell is never shortened again.
_CELL_MARKER_RESERVE: Final[int] = 32

# Upstream tool names (before server prefix / expose_as) that produce
# Statement Execution envelopes worth compacting.
_SQL_EXECUTE_UPSTREAM: Final[frozenset[str]] = frozenset(
    {
        "execute_sql",
        "execute_sql_read_only",
    }
)

# Type-tagged scalar cell wrappers used by the Statement Execution API.
# JSON_ARRAY uses the ``<type>_value`` form; PROTOBUF_ARRAY uses short tags.
_CELL_VALUE_KEYS: Final[tuple[str, ...]] = (
    "string_value",
    "long_value",
    "double_value",
    "boolean_value",
    "date_value",
    "timestamp_value",
    "binary_value",
    "decimal_value",
    "null_value",
    "str",
    "int",
    "long",
    "double",
    "float",
    "bool",
    "date",
    "timestamp",
    "binary",
    "decimal",
    "null",
)

# Tags whose presence means SQL NULL, regardless of the wrapped value
# (JSON_ARRAY emits ``{"null_value": "NULL_VALUE"}``). The managed SQL MCP's
# PROTOBUF_ARRAY sends NULL as an empty ``{}``, handled in ``_unwrap_cell``.
_NULL_CELL_KEYS: Final[frozenset[str]] = frozenset({"null_value", "null"})


def is_sql_execute_upstream(upstream_tool_name: str) -> bool:
    """True when *upstream_tool_name* is a managed-SQL execute tool."""
    return upstream_tool_name in _SQL_EXECUTE_UPSTREAM


def compact_sql_tool_text(
    text: str,
    *,
    row_cap: int = DEFAULT_ROW_CAP,
) -> str | None:
    """Rewrite a successful SQL tool JSON payload to the compact shape.

    Returns ``None`` when the payload is not a successful statement
    result (errors, pending states, unrecognised shapes) so the caller
    can leave the upstream bytes alone.
    """
    if row_cap < 1:
        raise ValueError(f"row_cap must be >= 1, got {row_cap}")
    compact = parse_sql_tool_text(text)
    if compact is None:
        return None
    return serialize_compact(bound_compact(compact, row_cap))


def parse_sql_tool_text(text: str) -> dict[str, Any] | None:
    """The compact shape of a successful SQL tool JSON payload, with every row and full cells.

    Returns ``None`` for errors, pending states and unrecognised shapes.
    An already compact payload is returned as it is.
    """
    try:
        parsed: Any = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    if _looks_compact(parsed):
        return parsed

    envelope = _find_statement_envelope(parsed)
    if envelope is None:
        return None
    if not _is_succeeded(envelope):
        return None

    columns = _extract_columns(envelope)
    if not columns:
        return None

    rows = _extract_rows(envelope, expected_cols=len(columns))
    total_rows = _extract_total_rows(envelope, fallback=len(rows))
    compact: dict[str, Any] = {
        "columns": columns,
        "rows": rows,
        "total_rows": total_rows,
        "truncated": False,
    }
    if _is_upstream_truncated(envelope):
        compact["truncated"] = True
        compact["total_rows_is_minimum"] = True
    statement_id = envelope.get("statement_id")
    if isinstance(statement_id, str) and statement_id:
        compact["statement_id"] = statement_id
    return compact


def compact_call_tool_result(
    result: Any,
    *,
    row_cap: int = DEFAULT_ROW_CAP,
) -> bool:
    """Mutate the first text content item of an MCP ``CallToolResult``.

    Returns ``True`` when the text was rewritten.  Fail-soft: any
    mismatch leaves *result* unchanged and returns ``False``.
    """
    content_list = getattr(result, "content", None)
    if not content_list:
        return False
    for item in content_list:
        text = getattr(item, "text", None)
        if not isinstance(text, str) or not text:
            continue
        compacted = compact_sql_tool_text(text, row_cap=row_cap)
        if compacted is None:
            return False
        try:
            item.text = compacted
        except Exception as exc:  # noqa: BLE001 -- fail soft on frozen models
            logger.debug(
                "[sql_compact] could not mutate content text (%s); leaving upstream bytes",
                type(exc).__name__,
            )
            return False
        return True
    return False


def _looks_compact(payload: dict[str, Any]) -> bool:
    columns = payload.get("columns")
    rows = payload.get("rows")
    return isinstance(columns, list) and isinstance(rows, list) and "manifest" not in payload


def bound_compact(compact: dict[str, Any], row_cap: int = DEFAULT_ROW_CAP) -> dict[str, Any]:
    """Keep up to *row_cap* rows within the size budget, shortening long cells only when whole rows don't fit."""
    rows = compact.get("rows")
    wanted = min(row_cap, len(rows)) if isinstance(rows, list) else 0
    if _rows_within_budget(compact) >= wanted:
        return _apply_row_cap(compact, row_cap)
    shortened = _shorten_cells(compact)
    return _apply_row_cap(shortened, min(row_cap, max(_rows_within_budget(shortened), 1)))


def _shorten_cells(compact: dict[str, Any]) -> dict[str, Any]:
    rows = compact.get("rows")
    if not isinstance(rows, list):
        return compact
    out = dict(compact)
    out["rows"] = [[_shorten_cell(cell) for cell in row] if isinstance(row, list) else row for row in rows]
    return out


def _shorten_cell(cell: Any) -> Any:
    if isinstance(cell, str):
        text = cell
    elif isinstance(cell, (dict, list)):
        text = json.dumps(cell, ensure_ascii=False, separators=(",", ":"))
    else:
        return cell
    if len(text) <= MAX_CELL_CHARS:
        return cell
    kept = MAX_CELL_CHARS - _CELL_MARKER_RESERVE
    return f"{text[:kept]}…[+{len(text) - kept} chars]"


def _rows_within_budget(compact: dict[str, Any]) -> int:
    """Number of leading rows whose serialized size fits ``MAX_RESULT_CHARS``."""
    rows = compact.get("rows")
    if not isinstance(rows, list):
        return 0
    # Header as it would look truncated, so bounding a bounded result keeps the same rows.
    header = {**compact, "rows": [], "truncated": True, "omitted_rows": max(_total_rows(compact), len(rows))}
    used = len(serialize_compact(header))
    for count, row in enumerate(rows):
        used += len(serialize_compact(row)) + 1
        if used > MAX_RESULT_CHARS:
            return count
    return len(rows)


def _total_rows(compact: dict[str, Any]) -> int:
    total_rows = compact.get("total_rows")
    return total_rows if isinstance(total_rows, int) else 0


def _apply_row_cap(compact: dict[str, Any], row_cap: int) -> dict[str, Any]:
    rows = compact.get("rows")
    if not isinstance(rows, list):
        return compact
    total_rows = compact.get("total_rows")
    if not isinstance(total_rows, int):
        total_rows = len(rows)
    if len(rows) <= row_cap and total_rows <= row_cap:
        out = dict(compact)
        out["total_rows"] = total_rows
        out["truncated"] = bool(compact.get("truncated", False))
        if not out["truncated"]:
            out.pop("omitted_rows", None)
        return out

    kept = rows[:row_cap]
    omitted = max(total_rows, len(rows)) - len(kept)
    out = dict(compact)
    out["rows"] = kept
    out["total_rows"] = max(total_rows, len(rows))
    out["truncated"] = True
    if omitted > 0 and not compact.get("total_rows_is_minimum"):
        out["omitted_rows"] = omitted
    return out


def serialize_compact(compact: Any) -> str:
    # Compact separators: every character here is paid as an input token
    # on every subsequent LLM call in the request.
    return json.dumps(compact, ensure_ascii=False, separators=(",", ":"))


def _find_statement_envelope(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Locate a Statement Execution body inside known managed-SQL wrappers."""
    if _is_statement_envelope(payload):
        return payload

    for candidate in (
        payload.get("statement_response"),
        (payload.get("content") or {}).get("statement_response") if isinstance(payload.get("content"), dict) else None,
    ):
        if isinstance(candidate, dict) and _is_statement_envelope(candidate):
            return candidate

    for attachments in (
        payload.get("queryAttachments"),
        (payload.get("content") or {}).get("queryAttachments") if isinstance(payload.get("content"), dict) else None,
    ):
        if not isinstance(attachments, list):
            continue
        for att in attachments:
            if not isinstance(att, dict):
                continue
            sr = att.get("statement_response")
            if isinstance(sr, dict) and _is_statement_envelope(sr):
                return sr
    return None


def _is_statement_envelope(payload: dict[str, Any]) -> bool:
    status = payload.get("status")
    return isinstance(status, dict) and isinstance(status.get("state"), str)


def _is_upstream_truncated(envelope: dict[str, Any]) -> bool:
    """True when the warehouse or the MCP server stopped returning rows early."""
    manifest = envelope.get("manifest")
    return envelope.get("truncated") is True or (isinstance(manifest, dict) and manifest.get("truncated") is True)


def _is_succeeded(envelope: dict[str, Any]) -> bool:
    status = envelope.get("status")
    if not isinstance(status, dict):
        return False
    return status.get("state") == "SUCCEEDED"


def _extract_columns(envelope: dict[str, Any]) -> list[str]:
    manifest = envelope.get("manifest")
    if not isinstance(manifest, dict):
        return []
    schema = manifest.get("schema")
    if not isinstance(schema, dict):
        return []
    columns_meta = schema.get("columns")
    if not isinstance(columns_meta, list):
        return []
    names: list[str] = []
    for col in columns_meta:
        if isinstance(col, dict):
            name = col.get("name")
            if isinstance(name, str) and name:
                names.append(name)
    return names


def _extract_rows(envelope: dict[str, Any], *, expected_cols: int) -> list[list[Any]]:
    result = envelope.get("result")
    if not isinstance(result, dict):
        return []
    raw_rows = result.get("data_array")
    if not isinstance(raw_rows, list):
        raw_rows = result.get("data_typed_array")
    if not isinstance(raw_rows, list):
        return []
    rows: list[list[Any]] = []
    for row in raw_rows:
        rows.append(_coerce_row(row, expected_cols))
    return rows


def _coerce_row(row: Any, expected_cols: int) -> list[Any]:
    if isinstance(row, list):
        return [_unwrap_cell(cell) for cell in row]
    if isinstance(row, dict):
        values = row.get("values")
        if isinstance(values, list):
            return [_unwrap_cell(cell) for cell in values]
    return [None] * expected_cols


def _unwrap_cell(cell: Any) -> Any:
    if cell is None or not isinstance(cell, dict):
        return cell
    if not cell:
        return None
    for key in _CELL_VALUE_KEYS:
        if key in cell:
            if key in _NULL_CELL_KEYS:
                return None
            return cell[key]
    return cell


def _extract_total_rows(envelope: dict[str, Any], *, fallback: int) -> int:
    manifest = envelope.get("manifest")
    if isinstance(manifest, dict):
        for key in ("total_row_count", "total_chunk_row_count"):
            value = manifest.get(key)
            if isinstance(value, int) and value >= 0:
                return value
        chunks = manifest.get("chunks")
        if isinstance(chunks, list) and chunks:
            first = chunks[0]
            if isinstance(first, dict):
                row_count = first.get("row_count")
                if isinstance(row_count, int) and row_count >= 0:
                    # Prefer an explicit total when present; otherwise the
                    # first chunk's count is a lower bound only -- callers
                    # that truncated server-side may still set total_*.
                    pass
    result = envelope.get("result")
    if isinstance(result, dict):
        row_count = result.get("row_count")
        if isinstance(row_count, int) and row_count >= 0:
            return row_count
    return fallback
