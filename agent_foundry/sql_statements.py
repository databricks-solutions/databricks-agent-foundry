"""SQL Statement Execution API helpers shared by brick deploy scripts.

Callers use these as module attributes (``sql_statements.execute_sql(...)``)
so a single ``monkeypatch.setattr(sql_statements, "execute_sql", ...)``
also covers the calls made by :func:`ensure_schema` and :func:`tag_schema`.
"""

from __future__ import annotations

import time
from typing import Any, cast


def quote_identifier(*parts: str) -> str:
    """Backtick-quote a dotted UC name (``catalog``.``schema``...), escaping embedded backticks."""
    return ".".join("`" + part.replace("`", "``") + "`" for part in parts)


def quote_dotted_name(name: str) -> str:
    """Back-quote every part of a dotted UC name as written in config.

    Dots inside back ticks do not split, and a part that is already
    back-quoted is unquoted first, so a name quoted in config and the same
    name left bare give the same result.
    """
    return quote_identifier(*(_unquote(part) for part in _split_dotted(name)))


def _split_dotted(name: str) -> list[str]:
    parts: list[str] = []
    start = 0
    in_quotes = False
    for index, char in enumerate(name):
        if char == "`":
            in_quotes = not in_quotes
        elif char == "." and not in_quotes:
            parts.append(name[start:index])
            start = index + 1
    parts.append(name[start:])
    return parts


def _unquote(part: str) -> str:
    if len(part) >= 2 and part.startswith("`") and part.endswith("`"):
        return part[1:-1].replace("``", "`")
    return part


def quote_string(value: str) -> str:
    """Single-quote *value* as a Databricks SQL string literal.

    Backslash is an escape character in Databricks string literals, so it
    is escaped before the quote; doubling ``'`` alone lets a trailing
    backslash break out of the literal.
    """
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def execute_sql(client: Any, warehouse_id: str, sql: str, timeout_seconds: int = 120) -> dict[str, Any]:
    """Execute a SQL statement and wait for completion.

    The Statement Execution API accepts wait_timeout between 5s-50s.
    If the statement doesn't complete within that window, we poll.
    """
    api_wait = min(timeout_seconds, 50)
    response = client.api_client.do(
        "POST",
        "/api/2.0/sql/statements",
        body={
            "warehouse_id": warehouse_id,
            "statement": sql,
            "wait_timeout": f"{api_wait}s",
            "disposition": "INLINE",
        },
    )

    status = response.get("status", {}).get("state", "UNKNOWN")
    if status == "SUCCEEDED":
        # ``api_client.do`` is typed as ``Any``; the SQL Statement
        # Execution API contract returns a mapping.
        return cast(dict[str, Any], response)

    if status in ("PENDING", "RUNNING"):
        statement_id = response.get("statement_id", "")
        return poll_statement(client, statement_id, timeout_seconds)

    error = response.get("status", {}).get("error", {})
    raise RuntimeError(f"SQL execution failed ({status}): {error.get('error_code', '')} - {error.get('message', '')}")


def poll_statement(client: Any, statement_id: str, timeout: int) -> dict[str, Any]:
    """Poll a running statement until completion."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        response = client.api_client.do("GET", f"/api/2.0/sql/statements/{statement_id}")
        status = response.get("status", {}).get("state", "UNKNOWN")
        if status == "SUCCEEDED":
            return cast(dict[str, Any], response)
        if status in ("FAILED", "CANCELED", "CLOSED"):
            error = response.get("status", {}).get("error", {})
            raise RuntimeError(f"SQL execution {status}: {error.get('error_code', '')} - {error.get('message', '')}")
        time.sleep(2)
    raise TimeoutError(f"Statement {statement_id} did not complete within {timeout}s")


def ensure_schema(client: Any, warehouse_id: str, catalog: str, schema: str) -> None:
    """Create the target schema if it doesn't already exist."""
    sql = f"CREATE SCHEMA IF NOT EXISTS {quote_identifier(catalog, schema)}"
    print(f"  Ensuring schema: {catalog}.{schema}")
    execute_sql(client, warehouse_id, sql)


def tag_schema(
    client: Any,
    warehouse_id: str,
    catalog: str,
    schema: str,
    tags: dict[str, str],
) -> None:
    """Apply UC tags to a schema via ALTER SCHEMA SET TAGS.

    Tags on schemas implicitly inherit to child objects (tables, views)
    during ABAC policy evaluation.  Failures are non-fatal (e.g. governed
    tag policies may restrict allowed values).
    """
    if not tags:
        return
    tag_pairs = ", ".join(f"{quote_string(k)} = {quote_string(v)}" for k, v in tags.items())
    sql = f"ALTER SCHEMA {quote_identifier(catalog, schema)} SET TAGS ({tag_pairs})"
    print(f"  Tagging schema {catalog}.{schema}: {tags}")
    try:
        execute_sql(client, warehouse_id, sql)
    except Exception as exc:
        print(f"  WARNING: failed to tag schema {catalog}.{schema}: {exc}")


__all__ = [
    "ensure_schema",
    "execute_sql",
    "poll_statement",
    "quote_dotted_name",
    "quote_identifier",
    "quote_string",
    "tag_schema",
]
