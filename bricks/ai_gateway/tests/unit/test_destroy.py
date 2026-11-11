"""Destroy deletes only brick-owned skills, then schemas the brick created when empty."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_foundry.bricks import DestroyContext
from bricks.ai_gateway.brick import AiGatewayBrick
from bricks.ai_gateway.scripts.skills import STATE_BRICK_TYPE, deploy_skills
from bricks.ai_gateway.tests.unit.conftest import SCHEMA, FakeWorkspace, write_skill

FOREIGN = f"skills/{SCHEMA}.foreign"


def _destroy(instance_dir: Path, ws: FakeWorkspace) -> Any:
    ctx = DestroyContext(instance_dir=instance_dir, target="dev", variables={"catalog": "main"}, ws=ws)
    return AiGatewayBrick().destroy(ctx)


def _deploy(instance_dir: Path, ws: FakeWorkspace) -> None:
    write_skill(instance_dir, "triage")
    deploy_skills(instance_dir, "dev", {"catalog": "main"}, ws=ws)  # type: ignore[arg-type]
    ws.ai_gateway.skills[FOREIGN] = "etag-x"


def test_destroy_deletes_only_skills_in_state(
    instance_dir: Path, ws: FakeWorkspace, state_store: dict[tuple[str, str, str, str], dict[str, Any]]
) -> None:
    _deploy(instance_dir, ws)
    [result] = _destroy(instance_dir, ws)
    assert result.status == "ok"
    assert [(ref.kind, ref.resource_id) for ref in result.deleted] == [("skill", f"{SCHEMA}.triage")]
    assert set(ws.ai_gateway.skills) == {FOREIGN}
    assert (instance_dir.name, STATE_BRICK_TYPE, "skills", "dev") not in state_store
    assert SCHEMA in ws.schemas.existing


@pytest.mark.usefixtures("state_store")
def test_destroy_drops_a_created_schema_only_when_empty(instance_dir: Path, ws: FakeWorkspace) -> None:
    ws.schemas.existing.clear()
    _deploy(instance_dir, ws)
    ws.schemas.non_empty.add(SCHEMA)
    [kept] = _destroy(instance_dir, ws)
    assert kept.status == "ok" and SCHEMA in ws.schemas.existing


@pytest.mark.usefixtures("state_store")
def test_destroy_reports_a_dropped_schema(instance_dir: Path, ws: FakeWorkspace) -> None:
    ws.schemas.existing.clear()
    _deploy(instance_dir, ws)
    [result] = _destroy(instance_dir, ws)
    assert ("uc_schema", SCHEMA) in [(ref.kind, ref.resource_id) for ref in result.deleted]


@pytest.mark.usefixtures("state_store")
def test_nothing_deployed_means_nothing_to_destroy(instance_dir: Path, ws: FakeWorkspace) -> None:
    [result] = _destroy(instance_dir, ws)
    assert result.status == "skipped"
