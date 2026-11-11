"""
Genie SQL Correctness Scorer
==============================
Code-based scorer that evaluates whether a Genie Space tool call
produced the expected SQL.  Compares generated SQL from the trace
against ``expectations["expected_sql"]``.

Uses whitespace-insensitive normalized comparison.  On mismatch,
reports token-level overlap as a coverage metric.

**Dev evaluation only** — requires ``expectations`` with ground-truth
SQL, which is not available in production monitoring.  Do not register
this scorer for production monitoring.

Span discovery
--------------
Genie reaches the agent two ways and the scorer supports both:

1. ``managed_genie`` MCP server (the default under ``mcp_only: true``).
   The MCP wrapper tags ``call_tool`` spans with
   ``tool_type="genie"`` and ``upstream_tool_name="query_space_<uuid>"``.
2. Legacy direct-mode ``create_genie_tool`` (only on
   ``mcp_only: false`` instances).  Wrapper span name contains
   ``"genie"`` and its outputs carry a structured ``sql`` key.

Discovery is attribute-first so it stays robust to operator-chosen
``name_prefix`` values; the name-substring branch is preserved for
back-compat with pre-fix traces.
"""

from __future__ import annotations

import fnmatch
import re
from typing import Any

from mlflow.entities import Feedback
from mlflow.entities.trace import Trace
from mlflow.genai.scorers import scorer

# Matches the ``"SQL: <stmt>"`` block that ``_format_genie_message``
# emits in ``payloads/agent/runtime/tools.py`` and that ``managed_genie``
# MCP responses propagate verbatim.  Captures up to the first blank
# line (which separates SQL from the rendered result table) or end of
# string, whichever comes first.
_SQL_PREFIX_RE = re.compile(r"SQL:\s*(SELECT\b[\s\S]*?)(?:\n\n|\Z)", re.IGNORECASE)


def _normalize_sql(sql: str) -> str:
    """Collapse whitespace and lowercase for comparison."""
    return re.sub(r"\s+", " ", sql.strip().lower())


def _is_genie_span(span: Any) -> bool:
    """True iff *span* represents a Genie tool invocation.

    Checked in priority order: explicit ``tool_type`` attribute,
    upstream MCP tool-name glob, then legacy span-name substring.
    """
    attributes = getattr(span, "attributes", None) or {}
    if attributes.get("tool_type") == "genie":
        return True
    upstream = attributes.get("upstream_tool_name")
    if isinstance(upstream, str) and fnmatch.fnmatchcase(upstream, "query_space*"):
        return True
    name = getattr(span, "name", "") or ""
    return "genie" in name.lower()


def _sql_from_outputs(outputs: Any) -> str | None:
    """Pull a SQL string from a span's ``outputs``.

    Prefers structured keys (legacy direct-mode wrapper writes
    ``outputs["sql"]``); falls back to parsing the ``"SQL: ..."``
    prefix that lives inside the Genie response payload (MCP path).
    """
    if isinstance(outputs, dict):
        for key in ("sql", "query", "generated_sql"):
            value = outputs.get(key)
            if isinstance(value, str) and value:
                return value
        # The MCP CallToolResult often surfaces as a dict whose payload
        # text lives under a single content key.  Stringify and let the
        # regex below pick out the SQL block.
        outputs = str(outputs)
    if isinstance(outputs, str):
        match = _SQL_PREFIX_RE.search(outputs)
        if match:
            return match.group(1).strip()
        if "SELECT" in outputs.upper():
            return outputs
    return None


def _extract_genie_sql(trace: Trace | None) -> str | None:
    """Extract the SQL query from a Genie tool call span output."""
    if trace is None:
        return None
    try:
        spans = trace.search_spans()
    except Exception:
        return None
    for span in spans:
        if not _is_genie_span(span):
            continue
        sql = _sql_from_outputs(getattr(span, "outputs", None))
        if sql:
            return sql
    return None


@scorer
def genie_sql_correctness(
    trace: Trace | None,
    expectations: dict[str, Any] | None = None,
) -> Feedback | None:
    """Score whether the Genie tool produced the expected SQL.

    Expects ``expectations["expected_sql"]`` to contain the ground-truth
    SQL string.

    Returns ``True`` if the normalized SQL matches, ``False`` otherwise.
    Returns ``None`` (harness records no assessment for this row) when
    the row has no ``expected_sql`` key, so the scorer opts out rather
    than failing the row.
    """
    if not expectations or "expected_sql" not in expectations:
        return None

    expected_sql = expectations["expected_sql"]
    actual_sql = _extract_genie_sql(trace)

    if actual_sql is None:
        return Feedback(
            value=False,
            rationale="No Genie SQL output found in trace spans.",
        )

    expected_norm = _normalize_sql(expected_sql)
    actual_norm = _normalize_sql(actual_sql)

    if expected_norm == actual_norm:
        return Feedback(
            value=True,
            rationale="Generated SQL matches expected SQL (normalized comparison).",
        )

    expected_tokens = set(expected_norm.split())
    actual_tokens = set(actual_norm.split())
    overlap = expected_tokens & actual_tokens
    coverage = len(overlap) / max(len(expected_tokens), 1)

    return Feedback(
        value=False,
        rationale=(
            f"SQL mismatch. Token overlap: {coverage:.0%}. "
            f"Expected (normalized): {expected_norm[:200]}... "
            f"Actual (normalized): {actual_norm[:200]}..."
        ),
    )
