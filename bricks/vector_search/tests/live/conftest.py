"""Vector indexes the deployed instance published (``vs_index_name__<index>``)."""

from __future__ import annotations

import pytest

from agent_foundry.testing.live import published_outputs

_OUTPUT = "vs_index_name"


@pytest.fixture(scope="session")
def index_names(live_variables: dict[str, str]) -> dict[str, str]:
    names = published_outputs(live_variables, _OUTPUT)
    assert names, f"vector_indexes brick published no {_OUTPUT}__<index>"
    return names


@pytest.fixture(scope="session")
def gone_index_names(gone_variables: dict[str, str]) -> dict[str, str]:
    names = published_outputs(gone_variables, _OUTPUT)
    assert names, f"no {_OUTPUT}__<index> was published before destroy"
    return names
