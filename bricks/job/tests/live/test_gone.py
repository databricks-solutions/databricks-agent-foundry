"""After destroy, every published job is deleted."""

from __future__ import annotations

from typing import Any

import pytest
from databricks.sdk.errors import InvalidParameterValue, NotFound

pytestmark = pytest.mark.gone


def _exists(ws: Any, job_id: int) -> bool:
    try:
        ws.jobs.get(job_id)
    except NotFound:
        return False
    except InvalidParameterValue as exc:
        # jobs/get answers a deleted id with "Job <id> does not exist."
        if "does not exist" in str(exc):
            return False
        raise
    return True


def test_jobs_are_deleted(live_ws: Any, gone_job_ids: dict[str, int]) -> None:
    remaining = {name: job_id for name, job_id in gone_job_ids.items() if _exists(live_ws, job_id)}
    assert not remaining, f"jobs still exist after destroy: {remaining}"
