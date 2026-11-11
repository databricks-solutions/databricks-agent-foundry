"""The scaffold's example UDF executes on the published warehouse."""

from __future__ import annotations

from typing import Any

from agent_foundry.testing.live import quote_identifier, run_sql


def test_scaffold_function_executes(
    live_ws: Any, live_variables: dict[str, str], scaffold_function: tuple[str, str, str]
) -> None:
    rows = run_sql(
        live_ws,
        live_variables["warehouse_id"],
        f"SELECT {quote_identifier(*scaffold_function)}(:text)",
        parameters={"text": "canary"},
    )
    assert rows == [["CANARY"]]
