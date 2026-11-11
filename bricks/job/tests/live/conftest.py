"""Jobs the deployed instance published (``job_id__<job>``)."""

from __future__ import annotations

import pytest

from agent_foundry.testing.live import published_outputs

_OUTPUT = "job_id"


@pytest.fixture(scope="session")
def job_ids(live_variables: dict[str, str]) -> dict[str, int]:
    ids = published_outputs(live_variables, _OUTPUT)
    assert ids, f"jobs brick published no {_OUTPUT}__<job>"
    return {name: int(job_id) for name, job_id in ids.items()}


@pytest.fixture(scope="session")
def gone_job_ids(gone_variables: dict[str, str]) -> dict[str, int]:
    ids = published_outputs(gone_variables, _OUTPUT)
    assert ids, f"no {_OUTPUT}__<job> was published before destroy"
    return {name: int(job_id) for name, job_id in ids.items()}
