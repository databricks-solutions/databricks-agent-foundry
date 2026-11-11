"""Genie spaces the deployed instance published (``genie_space_id__<space>``)."""

from __future__ import annotations

import pytest

from agent_foundry.testing.live import published_outputs

_OUTPUT = "genie_space_id"


@pytest.fixture(scope="session")
def space_ids(live_variables: dict[str, str]) -> dict[str, str]:
    ids = published_outputs(live_variables, _OUTPUT)
    assert ids, f"genie_spaces brick published no {_OUTPUT}__<space>"
    return ids


@pytest.fixture(scope="session")
def gone_space_ids(gone_variables: dict[str, str]) -> dict[str, str]:
    ids = published_outputs(gone_variables, _OUTPUT)
    assert ids, f"no {_OUTPUT}__<space> was published before destroy"
    return ids
