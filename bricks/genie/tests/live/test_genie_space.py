"""Each deployed Genie space answers its own benchmark questions over its own tables."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from databricks.sdk.service.dashboards import MessageStatus

# A fresh space can sit in ASKING_AI for minutes; a stuck conversation gets one new attempt.
_ANSWER_TIMEOUT = timedelta(minutes=10)
_ANSWER_ATTEMPTS = 2


def _first(value: Any) -> str:
    return "".join(value) if isinstance(value, list) else str(value or "")


def _benchmarks(serialized: dict[str, Any]) -> list[tuple[str, str]]:
    """(question, expected SQL) pairs from the space's serialized benchmarks."""
    pairs = []
    for item in serialized.get("benchmarks", {}).get("questions", []):
        question = _first(item.get("question"))
        answers = item.get("answer") or [{}]
        pairs.append((question, _first(answers[0].get("content")).strip()))
    return pairs


def _schema_tables(serialized: dict[str, Any]) -> list[str]:
    """``schema.table`` for every data source; Genie may drop the catalog part."""
    identifiers = [t.get("identifier", "") for t in serialized.get("data_sources", {}).get("tables", [])]
    return [ident.split(".", 1)[1].lower() for ident in identifiers if ident.count(".") == 2]


def _generated_sql(message: Any) -> str:
    for attachment in message.attachments or []:
        if attachment.query is not None and attachment.query.query:
            return str(attachment.query.query).strip()
    return ""


def _unquoted(sql: str) -> str:
    return sql.lower().replace("`", "")


def _ask(ws: Any, space_id: str, question: str) -> Any:
    """Answer from a new conversation per attempt; the last attempt's timeout propagates."""
    for _ in range(_ANSWER_ATTEMPTS - 1):
        try:
            return ws.genie.start_conversation_and_wait(space_id, question, timeout=_ANSWER_TIMEOUT)
        except TimeoutError:
            continue
    return ws.genie.start_conversation_and_wait(space_id, question, timeout=_ANSWER_TIMEOUT)


def _check_benchmark(ws: Any, space_id: str, question: str, expected_sql: str, tables: list[str]) -> list[str]:
    message = _ask(ws, space_id, question)
    if message.status != MessageStatus.COMPLETED:
        return [f"{question!r}: status {message.status}"]
    actual = _generated_sql(message)
    if not actual:
        return [f"{question!r}: no SQL generated"]
    expected_tables = [t for t in tables if t in _unquoted(expected_sql)]
    return [
        f"{question!r}: SQL does not reference {t}: {actual[:200]}"
        for t in expected_tables
        if t not in _unquoted(actual)
    ]


def test_spaces_answer_their_benchmarks(live_ws: Any, space_ids: dict[str, str]) -> None:
    failures: list[str] = []
    for name, space_id in space_ids.items():
        space = live_ws.genie.get_space(space_id, include_serialized_space=True)
        serialized = json.loads(space.serialized_space or "{}")
        benchmarks = _benchmarks(serialized)
        assert benchmarks, f"space {name} ({space_id}) has no benchmark questions"
        tables = _schema_tables(serialized)
        for question, expected_sql in benchmarks:
            failures.extend(f"{name}: {f}" for f in _check_benchmark(live_ws, space_id, question, expected_sql, tables))
    assert not failures, "\n".join(failures)
