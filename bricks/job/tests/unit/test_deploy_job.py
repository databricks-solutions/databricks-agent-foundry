from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from agent_foundry.loader import FoundryConfigError
from agent_foundry.state import load_state, save_state
from agent_foundry.testing.fakes import FakeWorkspaceClient
from bricks.job.scripts import deploy_job as deploy_module
from bricks.job.scripts.deploy_job import build_job_settings, deploy


def _write_job(job_dir: Path, *, extra: dict[str, Any] | None = None) -> None:
    (job_dir / "notebooks").mkdir(parents=True)
    (job_dir / "notebooks" / "ingest.py").write_text(
        "# Databricks notebook source\nprint('ok')\n",
        encoding="utf-8",
    )
    (job_dir / "job.yml").write_text(
        yaml.safe_dump(
            {
                "name": "${var.app_name}-ingest",
                "tasks": [
                    {
                        "task_key": "ingest",
                        "notebook_task": {
                            "notebook_path": "./notebooks/ingest.py",
                        },
                    }
                ],
                **(extra or {}),
            }
        ),
        encoding="utf-8",
    )


def _append_task(job_dir: Path, task: dict[str, Any]) -> None:
    """Add one more task to a job written by :func:`_write_job`."""
    config_path = job_dir / "job.yml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["tasks"].append(task)
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")


def test_build_settings_remaps_relative_notebook(tmp_path: Path) -> None:
    job_dir = tmp_path / "instances" / "demo" / "jobs" / "ingest"
    _write_job(job_dir)

    settings, uploads = build_job_settings(
        job_dir,
        "dev",
        {"app_name": "demo"},
    )

    notebook_path = settings["tasks"][0]["notebook_task"]["notebook_path"]
    assert notebook_path == "/Shared/agent-foundry/dev/demo/jobs/ingest/notebooks/ingest"
    assert settings["tasks"][0]["notebook_task"]["source"] == "WORKSPACE"
    assert uploads[0].local_path.name == "ingest.py"


def test_build_settings_pins_managed_by_tag(tmp_path: Path) -> None:
    job_dir = tmp_path / "instances" / "demo" / "jobs" / "ingest"
    _write_job(
        job_dir,
        extra={"tags": {"purpose": "example", "ai_solution_managed_by": "spoof"}},
    )
    settings, _ = build_job_settings(job_dir, "dev", {"app_name": "demo"})
    assert settings["tags"]["ai_solution_managed_by"] == "demo"
    assert settings["tags"]["purpose"] == "example"


def test_build_settings_remaps_notebook_inside_for_each(tmp_path: Path) -> None:
    """Fan-out bodies are deployed sources too (#406).

    ``for_each_task`` used to be an opaque passthrough dict, so its
    nested notebook reached the Jobs API as ``./notebooks/...`` and the
    file was never uploaded -- the fan-out failed at runtime.
    """
    job_dir = tmp_path / "instances" / "demo" / "jobs" / "ingest"
    _write_job(
        job_dir,
        extra={
            "tasks": [
                {
                    "task_key": "fan_out",
                    "for_each_task": {
                        "inputs": "{{tasks.prepare.values.items}}",
                        "concurrency": 8,
                        "task": {
                            "task_key": "one",
                            "notebook_task": {"notebook_path": "./notebooks/ingest.py"},
                        },
                    },
                }
            ]
        },
    )

    settings, uploads = build_job_settings(job_dir, "dev", {"app_name": "demo"})

    nested = settings["tasks"][0]["for_each_task"]["task"]["notebook_task"]
    assert nested["notebook_path"] == "/Shared/agent-foundry/dev/demo/jobs/ingest/notebooks/ingest"
    assert nested["source"] == "WORKSPACE"
    assert [upload.local_path.name for upload in uploads] == ["ingest.py"]


def test_build_settings_remaps_relative_sql_file(tmp_path: Path) -> None:
    """``sql_task.file.path`` keeps its suffix and skips /Workspace.

    The API addresses SQL files like notebooks (bare workspace path),
    not like ``spark_python_task.python_file``.
    """
    job_dir = tmp_path / "instances" / "demo" / "jobs" / "ingest"
    _write_job(job_dir)
    (job_dir / "sql").mkdir()
    (job_dir / "sql" / "refresh.sql").write_text("SELECT 1\n", encoding="utf-8")
    _append_task(
        job_dir,
        {
            "task_key": "refresh",
            "sql_task": {
                "warehouse_id": "abc123",
                "file": {"path": "./sql/refresh.sql"},
            },
        },
    )

    settings, uploads = build_job_settings(job_dir, "dev", {"app_name": "demo"})

    sql_file = settings["tasks"][1]["sql_task"]["file"]
    assert sql_file["path"] == "/Shared/agent-foundry/dev/demo/jobs/ingest/sql/refresh.sql"
    assert sql_file["source"] == "WORKSPACE"
    assert not any(upload.is_notebook for upload in uploads if upload.local_path.suffix == ".sql")


def test_same_notebook_in_two_tasks_uploads_once(tmp_path: Path) -> None:
    job_dir = tmp_path / "instances" / "demo" / "jobs" / "ingest"
    _write_job(job_dir)
    _append_task(
        job_dir,
        {
            "task_key": "again",
            "notebook_task": {"notebook_path": "./notebooks/ingest.py"},
        },
    )

    _, uploads = build_job_settings(job_dir, "dev", {"app_name": "demo"})

    assert len(uploads) == 1


def test_colliding_notebook_names_are_rejected(tmp_path: Path) -> None:
    """Notebook paths drop the suffix, so ``x.py`` and ``x.sql`` clash."""
    job_dir = tmp_path / "instances" / "demo" / "jobs" / "ingest"
    _write_job(job_dir)
    (job_dir / "notebooks" / "ingest.sql").write_text("SELECT 1\n", encoding="utf-8")
    _append_task(
        job_dir,
        {
            "task_key": "sql_notebook",
            "notebook_task": {"notebook_path": "./notebooks/ingest.sql"},
        },
    )

    with pytest.raises(ValueError, match="same workspace path"):
        build_job_settings(job_dir, "dev", {"app_name": "demo"})


def test_local_dbt_project_directory_is_rejected(tmp_path: Path) -> None:
    """A dbt project is a tree; the brick only uploads single files."""
    job_dir = tmp_path / "instances" / "demo" / "jobs" / "ingest"
    _write_job(
        job_dir,
        extra={
            "tasks": [
                {
                    "task_key": "dbt",
                    "dbt_task": {
                        "project_directory": "./dbt",
                        "commands": ["dbt run"],
                    },
                }
            ]
        },
    )

    with pytest.raises(FoundryConfigError, match="project_directory"):
        build_job_settings(job_dir, "dev", {"app_name": "demo"})


def test_dry_run_does_not_require_workspace(tmp_path: Path, capsys: Any) -> None:
    job_dir = tmp_path / "instances" / "demo" / "jobs" / "ingest"
    _write_job(job_dir)

    assert deploy(job_dir, "dev", {"app_name": "demo"}, dry_run=True) == 0
    assert '"name": "demo-ingest"' in capsys.readouterr().out


class _DeployJobs:
    def __init__(self) -> None:
        self.created: dict[str, Any] | None = None
        self.reset_job_id: int | None = None

    def list(self, *, name: str) -> list[Any]:
        return []

    def create(self, **kwargs: Any) -> Any:
        self.created = kwargs
        return SimpleNamespace(job_id=55)

    def reset(self, job_id: int, settings: Any) -> None:
        self.reset_job_id = job_id


class _DeployClient:
    def __init__(self) -> None:
        self.jobs = _DeployJobs()


def test_deploy_creates_and_saves_state(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    job_dir = tmp_path / "instances" / "demo" / "jobs" / "ingest"
    _write_job(job_dir)
    client = _DeployClient()
    saved: list[dict[str, Any]] = []
    monkeypatch.setattr(deploy_module, "load_state", lambda *args, **kwargs: {})
    monkeypatch.setattr(deploy_module, "_upload_sources", lambda *args: None)
    monkeypatch.setattr(
        deploy_module,
        "save_state",
        lambda *args, **kwargs: saved.append(args[5]),
    )

    assert deploy(job_dir, "dev", {"app_name": "demo"}, ws=client) == 0
    assert client.jobs.created is not None
    assert saved == [{"job_id": "55", "job_name": "demo-ingest"}]


def test_create_receives_typed_arguments(tmp_path: Path, monkeypatch: Any) -> None:
    """The SDK serialises each argument itself, so it needs the models.

    Passing ``JobSettings.as_dict()`` looks equivalent but flattens the
    nested models to plain dicts, and ``jobs.create`` then dies with
    "'dict' object has no attribute 'as_dict'".  A fake client that only
    records its kwargs cannot see the difference, so assert the types.
    """
    from databricks.sdk.service import jobs as jobs_sdk

    job_dir = tmp_path / "instances" / "demo" / "jobs" / "ingest"
    _write_job(job_dir)
    client = _DeployClient()
    monkeypatch.setattr(deploy_module, "load_state", lambda *args, **kwargs: {})
    monkeypatch.setattr(deploy_module, "_upload_sources", lambda *args: None)
    monkeypatch.setattr(deploy_module, "save_state", lambda *args, **kwargs: None)

    deploy(job_dir, "dev", {"app_name": "demo"}, ws=client)

    created = client.jobs.created
    assert created is not None
    assert all(isinstance(task, jobs_sdk.Task) for task in created["tasks"])


def test_table_update_condition_survives_job_settings(tmp_path: Path) -> None:
    """``from_dict`` drops conditions it cannot map to the API enum (#407).

    Asserting on the settings dict alone would pass with a value the SDK
    silently discards, so round-trip through ``JobSettings`` and read the
    typed trigger back.
    """
    from databricks.sdk.service import jobs as jobs_sdk

    job_dir = tmp_path / "instances" / "demo" / "jobs" / "ingest"
    _write_job(
        job_dir,
        extra={
            "trigger": {
                "table_update": {
                    "table_names": ["c.a.freshness_a", "c.b.freshness_b"],
                    "condition": "ANY_UPDATED",
                }
            }
        },
    )

    settings_dict, _ = build_job_settings(job_dir, "dev", {"app_name": "demo"})
    trigger = jobs_sdk.JobSettings.from_dict(settings_dict).trigger

    assert trigger is not None
    assert trigger.table_update is not None
    assert trigger.table_update.condition == jobs_sdk.Condition.ANY_UPDATED


def test_deploy_resets_job_from_state(tmp_path: Path, monkeypatch: Any) -> None:
    job_dir = tmp_path / "instances" / "demo" / "jobs" / "ingest"
    _write_job(job_dir)
    client = _DeployClient()
    monkeypatch.setattr(
        deploy_module,
        "load_state",
        lambda *args, **kwargs: {"job_id": "42", "job_name": "demo-ingest"},
    )
    monkeypatch.setattr(deploy_module, "_upload_sources", lambda *args: None)
    monkeypatch.setattr(deploy_module, "save_state", lambda *args, **kwargs: None)

    assert deploy(job_dir, "dev", {"app_name": "demo"}, ws=client) == 0
    assert client.jobs.reset_job_id == 42
    assert client.jobs.created is None


class _FakeJobs:
    def __init__(self) -> None:
        self.deleted: list[int] = []

    def delete(self, job_id: int) -> None:
        self.deleted.append(job_id)


class _FakeClient:
    def __init__(self) -> None:
        self.jobs = _FakeJobs()
        self.workspace = FakeWorkspaceClient().workspace


def test_destroy_deletes_job_and_state(tmp_path: Path) -> None:
    job_dir = tmp_path / "instances" / "demo" / "jobs" / "ingest"
    _write_job(job_dir)
    client = _FakeClient()
    save_state(client, "demo", "jobs", "ingest", "dev", {"job_id": "42", "job_name": "demo-ingest"})

    assert (
        deploy(
            job_dir,
            "dev",
            {"app_name": "demo"},
            action="destroy",
            ws=client,
        )
        == 0
    )
    assert client.jobs.deleted == [42]
    assert load_state(client, "demo", "jobs", "ingest", "dev") == {}
