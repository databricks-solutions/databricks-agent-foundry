from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

from agent_foundry.loader import FoundryConfigError
from bricks.job.scripts.validate_job import main, only_vs_index_outputs_missing, validate_job_config


def test_valid_notebook_job() -> None:
    config = validate_job_config(
        {
            "name": "docs-ingest",
            "tasks": [
                {
                    "task_key": "ingest",
                    "notebook_task": {"notebook_path": "/Shared/ingest"},
                }
            ],
            "trigger": {"file_arrival": {"url": "/Volumes/catalog/schema/sources/"}},
        }
    )
    assert config.tasks[0].notebook_task is not None


def test_accepts_python_wheel_passthrough() -> None:
    config = validate_job_config(
        {
            "name": "wheel-job",
            "tasks": [
                {
                    "task_key": "wheel",
                    "python_wheel_task": {
                        "package_name": "example",
                        "entry_point": "main",
                    },
                    "environment_key": "default",
                }
            ],
            "environments": [{"environment_key": "default", "spec": {"client": "1"}}],
        }
    )
    assert config.tasks[0].python_wheel_task is not None


def test_rejects_multiple_task_types() -> None:
    with pytest.raises(FoundryConfigError, match="exactly one"):
        validate_job_config(
            {
                "name": "ambiguous",
                "tasks": [
                    {
                        "task_key": "bad",
                        "notebook_task": {"notebook_path": "/Shared/a"},
                        "spark_python_task": {"python_file": "/Workspace/a.py"},
                    }
                ],
            }
        )


def test_rejects_schedule_and_trigger_together() -> None:
    with pytest.raises(FoundryConfigError, match="mutually exclusive"):
        validate_job_config(
            {
                "name": "both-modes",
                "tasks": [
                    {
                        "task_key": "n",
                        "notebook_task": {"notebook_path": "/Shared/a"},
                    }
                ],
                "schedule": {
                    "quartz_cron_expression": "0 0 8 * * ?",
                    "timezone_id": "UTC",
                },
                "trigger": {
                    "file_arrival": {"url": "/Volumes/c/s/v/"},
                },
            }
        )


def test_rejects_file_arrival_url_without_trailing_slash() -> None:
    with pytest.raises(FoundryConfigError, match="must end with '/'"):
        validate_job_config(
            {
                "name": "no-slash",
                "tasks": [
                    {
                        "task_key": "n",
                        "notebook_task": {"notebook_path": "/Shared/a"},
                    }
                ],
                "trigger": {"file_arrival": {"url": "/Volumes/c/s/v"}},
            }
        )


def _file_arrival_job(**timing: int) -> dict[str, object]:
    return {
        "name": "arrival-timing",
        "tasks": [{"task_key": "n", "notebook_task": {"notebook_path": "/Shared/a"}}],
        "trigger": {"file_arrival": {"url": "/Volumes/c/s/v/", **timing}},
    }


@pytest.mark.parametrize(
    "timing",
    [{"wait_after_last_change_seconds": 60}, {"min_time_between_triggers_seconds": 59}],
)
def test_rejects_file_arrival_timing_below_api_minimum(timing: dict[str, int]) -> None:
    with pytest.raises(FoundryConfigError, match="greater than"):
        validate_job_config(_file_arrival_job(**timing))


def test_accepts_file_arrival_timing_at_api_minimum() -> None:
    config = validate_job_config(
        _file_arrival_job(wait_after_last_change_seconds=61, min_time_between_triggers_seconds=60)
    )
    assert config.trigger is not None


def test_accepts_api_table_update_condition() -> None:
    config = validate_job_config(
        {
            "name": "freshness",
            "tasks": [
                {
                    "task_key": "n",
                    "notebook_task": {"notebook_path": "/Shared/a"},
                }
            ],
            "trigger": {
                "table_update": {
                    "table_names": ["c.a.freshness_a", "c.b.freshness_b"],
                    "condition": "ANY_UPDATED",
                }
            },
        }
    )
    assert config.trigger is not None
    assert config.trigger.table_update is not None
    assert config.trigger.table_update.condition == "ANY_UPDATED"


def test_rejects_non_api_table_update_condition() -> None:
    """The Jobs API enum is ``ANY_UPDATED``/``ALL_UPDATED`` (#407).

    The SDK drops unmappable values instead of raising, so an accepted
    ``ANY_UPDATE`` would reach the API as a missing ``condition``.
    """
    with pytest.raises(FoundryConfigError, match="condition"):
        validate_job_config(
            {
                "name": "freshness",
                "tasks": [
                    {
                        "task_key": "n",
                        "notebook_task": {"notebook_path": "/Shared/a"},
                    }
                ],
                "trigger": {
                    "table_update": {
                        "table_names": ["c.a.freshness_a"],
                        "condition": "ANY_UPDATE",
                    }
                },
            }
        )


def test_job_clusters_and_notifications() -> None:
    config = validate_job_config(
        {
            "name": "classic",
            "tasks": [
                {
                    "task_key": "n",
                    "job_cluster_key": "shared",
                    "notebook_task": {"notebook_path": "/Shared/a"},
                }
            ],
            "job_clusters": [
                {
                    "job_cluster_key": "shared",
                    "new_cluster": {
                        "spark_version": "15.4.x-scala2.12",
                        "node_type_id": "i3.xlarge",
                        "num_workers": 2,
                    },
                }
            ],
            "email_notifications": {"on_failure": ["ops@example.com"]},
            "queue": {"enabled": True},
            "performance_target": "COST_OPTIMIZED",
        }
    )
    assert config.job_clusters[0].job_cluster_key == "shared"
    assert config.email_notifications is not None


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Unresolved variable(s) in job.yml: ${var.vs_index_name__docs_search}", True),
        ("Unresolved variable(s) in job.yml: ${var.catalog}, ${var.vs_index_name__docs_search}", False),
        ("Invalid job.yml: tasks must not be empty", False),
    ],
)
def test_only_vs_index_outputs_missing(message: str, expected: bool) -> None:
    assert only_vs_index_outputs_missing(FoundryConfigError(message)) is expected


def _write_repo_with_job(root: Path, notebook_path: str) -> None:
    (root / "foundry.toml").write_text(
        textwrap.dedent(
            """
            schema_version = 2

            [targets.dev]
            host = "https://dev.cloud.databricks.com"
            catalog = "main"

            [[instances]]
            path = "instances/demo"
            """
        ).lstrip(),
        encoding="utf-8",
    )
    instance_dir = root / "instances" / "demo"
    job_dir = instance_dir / "jobs" / "ingest"
    job_dir.mkdir(parents=True)
    (instance_dir / "foundry.toml").write_text('name = "demo"\nschema_prefix = "demo_app"\n', encoding="utf-8")
    job = {"name": "ingest", "tasks": [{"task_key": "t", "notebook_task": {"notebook_path": notebook_path}}]}
    (job_dir / "job.yml").write_text(yaml.safe_dump(job), encoding="utf-8")


def test_cli_skips_a_job_waiting_for_a_vector_search_index(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_repo_with_job(tmp_path, "/Shared/${var.vs_index_name__docs}")

    rc = main(["--instance", "demo", "--project-root", str(tmp_path)])

    assert rc == 0
    assert "Validation SKIPPED" in capsys.readouterr().out


def test_cli_fails_on_other_unresolved_variables(tmp_path: Path) -> None:
    _write_repo_with_job(tmp_path, "/Shared/${var.not_defined}")

    assert main(["--instance", "demo", "--project-root", str(tmp_path)]) == 1
