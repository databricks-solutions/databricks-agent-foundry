from __future__ import annotations

from pathlib import Path

import pytest

from agent_foundry.bricks import DeployContext
from agent_foundry.state import save_state
from agent_foundry.testing.fakes import FakeWorkspaceClient
from bricks.job.brick import JobsBrick


def test_applies_to_job_config(tmp_path: Path) -> None:
    brick = JobsBrick()
    ctx = DeployContext(instance_dir=tmp_path, target="dev")
    assert not brick.applies_to(ctx)

    job_dir = tmp_path / "jobs" / "docs_ingest"
    job_dir.mkdir(parents=True)
    (job_dir / "job.yml").write_text("name: test\ntasks: []\n", encoding="utf-8")
    assert brick.applies_to(ctx)


def test_load_state_outputs_uses_nested_variable_name(tmp_path: Path) -> None:
    (tmp_path / "jobs" / "docs_ingest").mkdir(parents=True)
    ws = FakeWorkspaceClient()
    save_state(ws, tmp_path.name, "jobs", "docs_ingest", "dev", {"job_id": "123", "job_name": "docs-ingest"})

    outputs = JobsBrick().load_state_outputs(DeployContext(instance_dir=tmp_path, target="dev", ws=ws))
    assert outputs == {"job_id__docs_ingest": "123"}


def _write_job(tmp_path: Path, name: str, body: str) -> None:
    job_dir = tmp_path / "jobs" / name
    job_dir.mkdir(parents=True)
    (job_dir / "job.yml").write_text(body, encoding="utf-8")


def test_deploy_skips_docs_ingest_when_vs_index_unresolved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_job(
        tmp_path,
        "docs_ingest",
        "\n".join(
            [
                "name: docs-ingest",
                "tasks:",
                "  - task_key: ingest",
                "    notebook_task:",
                "      notebook_path: /Shared/ingest",
                "      base_parameters:",
                '        vs_index: "${var.vs_index_name__docs_search}"',
            ]
        ),
    )
    _write_job(
        tmp_path,
        "profile",
        "\n".join(
            [
                "name: profile",
                "tasks:",
                "  - task_key: profile",
                "    notebook_task:",
                "      notebook_path: /Shared/profile",
            ]
        ),
    )
    deployed: list[str] = []

    def _fake_deploy(job_dir: Path, *_a: object, **_k: object) -> int:
        deployed.append(job_dir.name)
        return 0

    monkeypatch.setattr("bricks.job.scripts.deploy_job.deploy", _fake_deploy)

    results = JobsBrick().deploy(DeployContext(instance_dir=tmp_path, target="dev", variables={}))
    by_name = {r.name: r for r in results}
    assert by_name["docs_ingest"].status == "skipped"
    assert by_name["docs_ingest"].rc == 0
    assert by_name["profile"].status == "ok"
    assert deployed == ["profile"]


def test_deploy_records_validate_error_for_other_unresolved_vars(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_job(
        tmp_path,
        "broken",
        "\n".join(
            [
                "name: broken",
                "tasks:",
                "  - task_key: t",
                "    notebook_task:",
                '      notebook_path: "${var.missing_notebook}"',
            ]
        ),
    )
    monkeypatch.setattr(
        "bricks.job.scripts.deploy_job.deploy",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not deploy")),
    )

    results = JobsBrick().deploy(DeployContext(instance_dir=tmp_path, target="dev", variables={}))
    assert len(results) == 1
    assert results[0].status == "validate_error"
    assert results[0].rc == 1
