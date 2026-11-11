"""Each published index comes online over a change-data-feed source and answers a query."""

from __future__ import annotations

import time
from typing import Any

from databricks.sdk.errors import BadRequest, NotFound

from agent_foundry.testing.live import quote_identifier, run_sql

# Deploy triggers the first sync but does not wait for it.
_READY_TIMEOUT_SECONDS = 30 * 60
_READY_POLL_SECONDS = 20
# A question the data_domains scaffold docs table (tables/01_docs_corpus.sql) answers.
_QUERY = "What is Unity Catalog?"


def _still_provisioning(exc: Exception) -> bool:
    """A fresh index answers NotFound, then BadRequest 'not ready', before it serves queries."""
    return isinstance(exc, NotFound) or (isinstance(exc, BadRequest) and "not ready" in str(exc))


def _query_when_ready(ws: Any, index_name: str) -> Any:
    """Query until the index answers; ``status.ready`` turns true before queries are served."""
    # A missing index fails here at once instead of after the deadline.
    ws.vector_search_indexes.get_index(index_name)
    deadline = time.monotonic() + _READY_TIMEOUT_SECONDS
    while True:
        try:
            return ws.vector_search_indexes.query_index(
                index_name, columns=["id", "content"], query_text=_QUERY, num_results=1
            )
        except (BadRequest, NotFound) as exc:
            if not _still_provisioning(exc):
                raise
            assert time.monotonic() < deadline, f"{index_name} not queryable after {_READY_TIMEOUT_SECONDS}s: {exc}"
        time.sleep(_READY_POLL_SECONDS)


def test_indexes_come_online_and_answer(live_ws: Any, index_names: dict[str, str]) -> None:
    for name, index_name in index_names.items():
        hits = _query_when_ready(live_ws, index_name)
        assert hits.result and hits.result.data_array, f"{name}: {index_name} returned no results"


def test_source_tables_have_change_data_feed(
    live_ws: Any, live_variables: dict[str, str], index_names: dict[str, str]
) -> None:
    for name, index_name in index_names.items():
        spec = live_ws.vector_search_indexes.get_index(index_name).delta_sync_index_spec
        assert spec and spec.source_table, f"{name}: {index_name} is not a Delta Sync index"
        rows = run_sql(
            live_ws,
            live_variables["warehouse_id"],
            f"SHOW TBLPROPERTIES {quote_identifier(*spec.source_table.split('.'))} ('delta.enableChangeDataFeed')",
        )
        assert rows and str(rows[0][1]).lower() == "true", f"{spec.source_table} has no change data feed: {rows}"
