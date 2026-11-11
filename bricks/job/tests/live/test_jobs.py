"""Each published job exists with its trigger paused, and one manual run succeeds."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from databricks.sdk.service.jobs import PauseStatus, RunResultState

_RUN_TIMEOUT = timedelta(minutes=30)


def _pause_status(settings: Any) -> PauseStatus | None:
    for trigger in (settings.trigger, settings.schedule, settings.continuous):
        if trigger is not None:
            return trigger.pause_status
    return None


def test_jobs_exist_paused(live_ws: Any, job_ids: dict[str, int]) -> None:
    unpaused = {}
    for name, job_id in job_ids.items():
        status = _pause_status(live_ws.jobs.get(job_id).settings)
        if status not in (None, PauseStatus.PAUSED):
            unpaused[name] = status
    assert not unpaused, f"jobs deployed unpaused: {unpaused}"


def test_one_manual_run_succeeds(live_ws: Any, job_ids: dict[str, int]) -> None:
    name, job_id = next(iter(sorted(job_ids.items())))
    run = live_ws.jobs.run_now_and_wait(job_id, timeout=_RUN_TIMEOUT)
    assert run.state and run.state.result_state == RunResultState.SUCCESS, f"{name}: run {run.run_id} {run.state}"
