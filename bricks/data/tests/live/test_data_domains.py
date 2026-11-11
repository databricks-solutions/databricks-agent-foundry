"""Each scaffolded data domain deployed its schema, views, rows and UC comments."""

from __future__ import annotations

from typing import Any

from agent_foundry.testing.live import quote_identifier, run_sql

# The data_domains scaffold's views (views/denormalized, views/pre_aggregated).
_SCAFFOLD_VIEWS = ("catalog_objects", "schema_summary")


def _tables(ws: Any, warehouse_id: str, qualified: str) -> dict[str, str]:
    catalog, schema = qualified.split(".", 1)
    rows = run_sql(
        ws,
        warehouse_id,
        f"SELECT table_name, comment FROM {quote_identifier(catalog, 'information_schema', 'tables')} "
        "WHERE table_schema = :schema",
        parameters={"schema": schema},
    )
    return {row[0]: row[1] or "" for row in rows}


def test_views_exist_with_comments(live_ws: Any, live_variables: dict[str, str], domain_schemas: list[str]) -> None:
    for qualified in domain_schemas:
        tables = _tables(live_ws, live_variables["warehouse_id"], qualified)
        missing = [view for view in _SCAFFOLD_VIEWS if view not in tables]
        assert not missing, f"{qualified} is missing views {missing}; has {sorted(tables)}"
        uncommented = [view for view in _SCAFFOLD_VIEWS if not tables[view].strip()]
        assert not uncommented, f"{qualified}: metadata/01_comments.sql left {uncommented} without a comment"


def test_views_return_rows(live_ws: Any, live_variables: dict[str, str], domain_schemas: list[str]) -> None:
    for qualified in domain_schemas:
        catalog, schema = qualified.split(".", 1)
        for view in _SCAFFOLD_VIEWS:
            rows = run_sql(
                live_ws,
                live_variables["warehouse_id"],
                f"SELECT COUNT(*) FROM {quote_identifier(catalog, schema, view)}",
            )
            assert int(rows[0][0]) > 0, f"{qualified}.{view} returned no rows"
