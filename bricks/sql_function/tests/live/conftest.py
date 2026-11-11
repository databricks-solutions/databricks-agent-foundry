"""The scaffold's UC function, located the way functions/example.sql names it."""

from __future__ import annotations

import pytest

# functions/example.sql: `${var.catalog}`.`${var.agent_schema_name}`.example_fn
_SCAFFOLD_FUNCTION = "example_fn"


def _function_parts(variables: dict[str, str]) -> tuple[str, str, str]:
    return variables["catalog"], variables["agent_schema_name"], _SCAFFOLD_FUNCTION


@pytest.fixture(scope="session")
def scaffold_function(live_variables: dict[str, str]) -> tuple[str, str, str]:
    return _function_parts(live_variables)


@pytest.fixture(scope="session")
def gone_scaffold_function(gone_variables: dict[str, str]) -> tuple[str, str, str]:
    return _function_parts(gone_variables)
