"""
Tool Output Renderers
======================
Registry of optional post-processors that emit additional UI-targeted
markdown after a tool returns.

Renderers are matched by tool-name regex.  Each matched renderer
receives the tool name, the (best-effort parsed) tool input arguments,
and the raw tool output, and returns either a string (rendered as an
extra ``message`` text part to the UI stream by
:mod:`payloads.agent.runtime.stream_utils`) or ``None`` (no extra rendering).

The original tool-result item is **never** modified; renderers are
purely additive.

The registry ships **with no default registrations**.  SQL warehouse
results are rendered client-side directly inside the Tool panel by the
``tool.tsx`` override (``payloads/agent/ui/overrides/client/src/components/
elements/tool.tsx``) -- duplicating the same rendering as a fenced
``message`` part would cause the table to appear twice in the chat UI.
The :func:`render_sql_warehouse_output` function is kept as a reference
implementation for future tool-output renderers (e.g. Genie, UC
functions) that want to emit fences instead of relying on the Tool
panel; downstream apps can ``register(...)`` it explicitly.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable

logger = logging.getLogger(__name__)

# A renderer takes (tool_name, parsed_arguments_or_None, output_value)
# and returns the markdown to append, or ``None`` to skip.
ToolOutputRenderer = Callable[[str, Any, Any], str | None]


@dataclass(frozen=True)
class _Registration:
    pattern: re.Pattern[str]
    fn: ToolOutputRenderer


_REGISTRATIONS: list[_Registration] = []


def register(pattern: re.Pattern[str], fn: ToolOutputRenderer) -> None:
    """Register a renderer keyed by a compiled tool-name regex.

    Renderers are evaluated in registration order; the first match
    wins.  The default ``sql_warehouse__execute_sql_*`` registration
    is added at module import time.
    """
    _REGISTRATIONS.append(_Registration(pattern, fn))


def _iter_matches(tool_name: str) -> Iterable[_Registration]:
    for reg in _REGISTRATIONS:
        if reg.pattern.search(tool_name):
            yield reg


def render_extra_text_part(
    tool_name: str,
    tool_arguments: Any,
    tool_output: Any,
) -> str | None:
    """Run the first matching renderer for *tool_name*.

    Renderer exceptions are swallowed and logged at WARNING --
    rendering is best-effort, the raw Tool panel always stays
    available for debugging.
    """
    for reg in _iter_matches(tool_name):
        try:
            rendered = reg.fn(tool_name, tool_arguments, tool_output)
        except Exception as exc:
            logger.warning(
                "[tool_renderer] %r raised %s: %s; skipping extra rendering for tool %r",
                reg.fn.__qualname__,
                type(exc).__name__,
                exc,
                tool_name,
            )
            return None
        if rendered:
            return rendered
    return None


# ---------------------------------------------------------------------------
# Default: SQL warehouse Statement Execution API renderer
# ---------------------------------------------------------------------------
# Emits two fenced blocks consumed by the chat UI's fence-dispatcher
# (see payloads/agent/ui/overrides/client/src/components/elements/response.tsx
# and the handlers registered there for ``sql-download`` and
# ``table-download``).

_SQL_WAREHOUSE_TOOL_RE = re.compile(r"sql_warehouse__execute_sql")


def _b64_encode_str(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


_MCP_TEXT_BLOCK_TYPES = frozenset({"text", "input_text"})


def _is_mcp_text_block(value: Any) -> bool:
    """Detect a text content-block dict shape.

    Two shapes appear in practice:

    - ``{"type": "text", "text": "..."}`` -- MCP-native
      (``agents.mcp.util`` ``ToolOutputTextDict``).
    - ``{"type": "input_text", "text": "..."}`` -- Responses API content
      block, which the SDK emits when forwarding MCP tool returns into
      the chat transcript via ``ResponseInputContent``.

    A SQL-warehouse Statement Execution envelope, by contrast, has a
    ``status``/``manifest``/``result`` triple at the top level.
    """
    return isinstance(value, dict) and value.get("type") in _MCP_TEXT_BLOCK_TYPES and isinstance(value.get("text"), str)


def _coerce_envelope(output: Any) -> dict[str, Any] | None:
    """Best-effort coerce a tool output to the Statement Execution
    API envelope dict.

    Recognised shapes:

    - bare envelope ``dict`` with ``status``/``manifest``: returned as-is.
    - MCP single-content-block ``{"type": "text", "text": "<json>"}``:
      the inner ``text`` is parsed.  This is the OpenAI Agents SDK's
      collapse of a single-item ``result.content`` list.
    - JSON string of a dict: parsed.
    - MCP content-block list ``[{"type": "text", "text": "<json>"}, ...]``
      (the SDK keeps the list when ``result.content`` has >1 item):
      the first ``{"text": ...}`` block whose text parses as a dict
      wins.

    Returns ``None`` for any unrecognised or malformed shape.
    """
    if _is_mcp_text_block(output):
        try:
            parsed = json.loads(output["text"])
        except (TypeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None
    if isinstance(output, dict):
        return output
    if isinstance(output, str):
        try:
            parsed = json.loads(output)
        except (TypeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None
    if isinstance(output, list):
        for block in output:
            if not isinstance(block, dict):
                continue
            text = block.get("text")
            if isinstance(text, str):
                try:
                    parsed = json.loads(text)
                except (TypeError, ValueError):
                    continue
                if isinstance(parsed, dict):
                    return parsed
    return None


def _extract_query(arguments: Any) -> str | None:
    """Pull the SQL ``query`` field out of the tool's arguments.

    Arguments may arrive as a JSON string (the OpenAI Agents SDK
    forwards them verbatim) or as a pre-parsed dict.
    """
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except (TypeError, ValueError):
            return None
    if isinstance(arguments, dict):
        for key in ("query", "statement", "sql"):
            value = arguments.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return None


def _extract_columns(envelope: dict[str, Any]) -> list[str]:
    columns_meta = envelope.get("manifest", {}).get("schema", {}).get("columns")
    if not isinstance(columns_meta, list):
        return []
    names: list[str] = []
    for col in columns_meta:
        if isinstance(col, dict):
            name = col.get("name")
            if isinstance(name, str):
                names.append(name)
    return names


def _extract_rows(envelope: dict[str, Any]) -> list[list[Any]]:
    data = envelope.get("result", {}).get("data_array")
    if not isinstance(data, list):
        return []
    rows: list[list[Any]] = []
    for row in data:
        if isinstance(row, list):
            rows.append(row)
    return rows


def _is_succeeded(envelope: dict[str, Any]) -> bool:
    status = envelope.get("status", {})
    state = status.get("state") if isinstance(status, dict) else None
    return state == "SUCCEEDED"


def render_sql_warehouse_output(
    tool_name: str,  # noqa: ARG001 -- part of the renderer signature
    tool_arguments: Any,
    tool_output: Any,
) -> str | None:
    """Render an SQL warehouse Statement Execution API result as
    ``sql-download`` + ``table-download`` fences.

    Returns ``None`` (and does not emit) when:

    - the envelope cannot be parsed,
    - the statement did not succeed,
    - or the result has no schema/rows (e.g. a DDL statement).

    The original tool-result part is left untouched in either case.
    """
    envelope = _coerce_envelope(tool_output)
    if envelope is None or not _is_succeeded(envelope):
        return None

    columns = _extract_columns(envelope)
    rows = _extract_rows(envelope)
    if not columns:
        return None

    query = _extract_query(tool_arguments)
    parts: list[str] = []

    if query:
        sql_payload_b64 = _b64_encode_str(query)
        parts.append(
            f"```sql-download:query.sql:{sql_payload_b64}\n{query}\n```",
        )

    table_payload = json.dumps(
        {
            "columns": columns,
            "rows": rows,
            "totalRows": len(rows),
        },
        sort_keys=True,
    )
    table_payload_b64 = _b64_encode_str(table_payload)
    parts.append(f"```table-download:result.csv:{table_payload_b64}\n```")

    if not parts:
        return None
    # Trailing blank line is REQUIRED for callers that DO register this
    # renderer.  The chat UI's part transformer
    # (``databricks-message-part-transformers.ts``) concatenates
    # consecutive ``text`` parts as raw strings; without a paragraph
    # break here, the next part's text would butt up against the closing
    # ``` of our last fence and Streamdown would parse the result as one
    # giant unclosed code block (swallowing any subsequent chart fence
    # the model emits in the same turn).
    return "\n\n".join(parts) + "\n\n"
