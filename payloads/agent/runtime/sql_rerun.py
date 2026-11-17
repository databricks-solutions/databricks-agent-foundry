"""Rerun a SQL query through the managed SQL MCP server's read-only tool.

Shared by the tools that need a full result the model never sees
(``_read_result``, ``_chart``): they rerun the model's query as the user and
parse the uncompacted rows.  The warehouse answers an identical query from
its result cache.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any, Final

from pydantic import ValidationError

from payloads.agent.runtime.mcp_wrapper import FilteredMCPServerStreamableHttp
from payloads.agent.runtime.sql_result_compact import parse_sql_tool_text

logger = logging.getLogger(__name__)

MANAGED_SQL_PATH: Final = "/api/2.0/mcp/sql"
# Read-only, so a rerun can never repeat a change.
READ_ONLY_SQL_TOOL: Final = "execute_sql_read_only"
MAX_QUERY_CHARS: Final = 20_000
_ERROR_PREVIEW_CHARS: Final = 500
_UNAVAILABLE: Final = "Error: the SQL tool is unavailable right now; continue without it."
_VALUE_ERROR_PREFIX: Final = "Value error, "


class RerunError(Exception):
    """A message for the model about a rerun that produced no result."""


def invalid_args_message(exc: ValidationError, usage: str) -> str:
    """Names each wrong argument; inputs are left out so the query text isn't echoed back."""
    problems = []
    for error in exc.errors(include_url=False, include_input=False):
        field = ".".join(str(part) for part in error["loc"])
        message = error["msg"].removeprefix(_VALUE_ERROR_PREFIX)
        problems.append(f"`{field}`: {message}" if field else message)
    return f"Error: {'; '.join(problems)}. {usage}"


def find_read_only_sql_tool(
    mcp_servers: Sequence[object],
) -> tuple[FilteredMCPServerStreamableHttp, str] | None:
    """The first managed SQL server whose allow-list admits the read-only tool, with its visible name."""
    for server in mcp_servers:
        if not isinstance(server, FilteredMCPServerStreamableHttp):
            continue
        if not server.server_url.rstrip("/").endswith(MANAGED_SQL_PATH):
            continue
        tool_name = server.visible_name(READ_ONLY_SQL_TOOL)
        if tool_name is not None:
            return server, tool_name
    return None


def _result_text(result: Any) -> str:
    for item in getattr(result, "content", None) or []:
        text = getattr(item, "text", None)
        if isinstance(text, str) and text:
            return text
    return ""


async def rerun_query(
    server: FilteredMCPServerStreamableHttp,
    tool_name: str,
    query: str,
    ctx: Any,
) -> dict[str, Any]:
    """The parsed, uncompacted result of *query*; raises :class:`RerunError` with a message for the model."""
    try:
        response = await server.call_tool_unbounded(tool_name, {"query": query}, ctx)
    except Exception as exc:  # noqa: BLE001 -- MCP transport errors vary; the model gets a plain message
        logger.warning("Read-only SQL rerun failed: %s", type(exc).__name__)
        raise RerunError(_UNAVAILABLE) from exc
    text = _result_text(response)
    result = None if getattr(response, "isError", False) else parse_sql_tool_text(text)
    if result is None:
        raise RerunError(f"Error: the query returned no result to read: {text[:_ERROR_PREVIEW_CHARS]}")
    return result
