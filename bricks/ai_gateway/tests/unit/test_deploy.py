"""Skills deploy: create, skip unchanged, git wins, prune brick-owned skills."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from databricks.sdk.errors.base import DatabricksError

from bricks.ai_gateway.scripts.skills import STATE_BRICK_TYPE, deploy_skills
from bricks.ai_gateway.tests.unit.conftest import SCHEMA, FakeWorkspace, skill_md, write_skill

VARS = {"catalog": "main"}
FQN = f"{SCHEMA}.triage"
NAME = f"skills/{FQN}"
ROOT = "/Skills/main/skills/triage"


def _deploy(instance_dir: Path, ws: FakeWorkspace) -> Any:
    return deploy_skills(instance_dir, "dev", VARS, ws=ws)  # type: ignore[arg-type]


def _state(store: dict[tuple[str, str, str, str], dict[str, Any]], instance_dir: Path) -> dict[str, Any]:
    return store[(instance_dir.name, STATE_BRICK_TYPE, "skills", "dev")]


def _finalizes(ws: FakeWorkspace) -> int:
    return sum(call.startswith("finalize") for call in ws.ai_gateway.calls)


@pytest.mark.usefixtures("state_store")
def test_missing_skill_is_created_and_uploaded(instance_dir: Path, ws: FakeWorkspace) -> None:
    write_skill(instance_dir, "triage", {"SKILL.md": skill_md("triage"), "refs/a.md": "A"})
    report = _deploy(instance_dir, ws)
    assert report.rc == 0 and report.changed == [FQN]
    assert ws.ai_gateway.calls == ["create triage", f"finalize {NAME}"]
    assert sorted(ws.file_store) == [f"{ROOT}/SKILL.md", f"{ROOT}/refs/a.md"]


def test_state_records_etag_and_hash(
    instance_dir: Path, ws: FakeWorkspace, state_store: dict[tuple[str, str, str, str], dict[str, Any]]
) -> None:
    write_skill(instance_dir, "triage")
    _deploy(instance_dir, ws)
    state = _state(state_store, instance_dir)
    assert state["schema"] == SCHEMA
    assert state["created_schemas"] == []
    record = state["skills"][FQN]
    assert record["etag"] == ws.ai_gateway.skills[NAME]
    assert len(record["content_hash"]) == 64


@pytest.mark.usefixtures("state_store")
def test_unchanged_skill_is_not_uploaded_and_keeps_its_etag(instance_dir: Path, ws: FakeWorkspace) -> None:
    write_skill(instance_dir, "triage")
    _deploy(instance_dir, ws)
    etag = ws.ai_gateway.skills[NAME]
    report = _deploy(instance_dir, ws)
    assert report.changed == [] and _finalizes(ws) == 1
    assert ws.ai_gateway.skills[NAME] == etag


@pytest.mark.usefixtures("state_store")
def test_local_change_is_published(instance_dir: Path, ws: FakeWorkspace) -> None:
    write_skill(instance_dir, "triage")
    _deploy(instance_dir, ws)
    write_skill(instance_dir, "triage", {"SKILL.md": skill_md("triage", body="New steps.")})
    report = _deploy(instance_dir, ws)
    assert report.changed == [FQN] and report.overwritten == []
    assert b"New steps." in ws.file_store[f"{ROOT}/SKILL.md"]


@pytest.mark.usefixtures("state_store")
def test_remote_edit_is_overwritten_and_reported(instance_dir: Path, ws: FakeWorkspace) -> None:
    write_skill(instance_dir, "triage")
    _deploy(instance_dir, ws)
    ws.file_store[f"{ROOT}/SKILL.md"] = skill_md("triage", body="UI edit.").encode()
    ws.file_store[f"{ROOT}/extra.md"] = b"added in the UI"
    ws.ai_gateway.edit_remotely(NAME)
    report = _deploy(instance_dir, ws)
    assert report.overwritten == [FQN]
    assert report.removed_files == {FQN: ["extra.md"]}
    assert sorted(ws.file_store) == [f"{ROOT}/SKILL.md"]
    assert b"UI edit." not in ws.file_store[f"{ROOT}/SKILL.md"]


@pytest.mark.usefixtures("state_store")
def test_remote_refinalize_with_equal_content_only_refreshes_the_etag(instance_dir: Path, ws: FakeWorkspace) -> None:
    write_skill(instance_dir, "triage")
    _deploy(instance_dir, ws)
    ws.ai_gateway.edit_remotely(NAME)
    etag = ws.ai_gateway.skills[NAME]
    report = _deploy(instance_dir, ws)
    assert report.changed == [] and _finalizes(ws) == 1
    assert ws.ai_gateway.skills[NAME] == etag
    assert _deploy(instance_dir, ws).changed == []


@pytest.mark.usefixtures("state_store")
def test_failure_after_create_removes_the_half_created_skill(instance_dir: Path, ws: FakeWorkspace) -> None:
    write_skill(instance_dir, "triage")
    ws.ai_gateway.finalize_error = DatabricksError("bad frontmatter")
    report = _deploy(instance_dir, ws)
    assert report.rc == 1 and "bad frontmatter" in report.errors[0]
    assert NAME not in ws.ai_gateway.skills
    assert ws.ai_gateway.calls[-1] == f"delete {NAME}"


@pytest.mark.usefixtures("state_store")
def test_one_invalid_folder_blocks_every_write(instance_dir: Path, ws: FakeWorkspace) -> None:
    write_skill(instance_dir, "triage")
    write_skill(instance_dir, "broken", {"SKILL.md": "no frontmatter"})
    report = _deploy(instance_dir, ws)
    assert report.rc == 1 and ws.ai_gateway.calls == []


@pytest.mark.usefixtures("state_store")
def test_removed_folder_prunes_only_brick_owned_skills(instance_dir: Path, ws: FakeWorkspace) -> None:
    write_skill(instance_dir, "triage")
    folder = write_skill(instance_dir, "billing")
    _deploy(instance_dir, ws)
    ws.ai_gateway.skills[f"skills/{SCHEMA}.foreign"] = "etag-x"
    for path in folder.iterdir():
        path.unlink()
    folder.rmdir()
    report = _deploy(instance_dir, ws)
    assert report.pruned == [f"{SCHEMA}.billing"]
    assert set(ws.ai_gateway.skills) == {NAME, f"skills/{SCHEMA}.foreign"}


def test_missing_schema_is_created_and_recorded(
    instance_dir: Path, ws: FakeWorkspace, state_store: dict[tuple[str, str, str, str], dict[str, Any]]
) -> None:
    ws.schemas.existing.clear()
    write_skill(instance_dir, "triage")
    _deploy(instance_dir, ws)
    assert SCHEMA in ws.schemas.existing
    assert _state(state_store, instance_dir)["created_schemas"] == [SCHEMA]


def test_dry_run_writes_nothing(instance_dir: Path, ws: FakeWorkspace) -> None:
    write_skill(instance_dir, "triage")
    report = deploy_skills(instance_dir, "dev", VARS, dry_run=True, ws=ws)  # type: ignore[arg-type]
    assert report.rc == 0 and ws.ai_gateway.calls == []
