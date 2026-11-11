"""Brick skills the agent's skills config cannot reach are reported."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from payloads.agent.contracts import AgentPayloadSpec
from payloads.agent.deploy.gateway_reach import unreachable_brick_skills

VARS = {"catalog": "main"}


@pytest.fixture
def instance_dir(tmp_path: Path) -> Path:
    brick = tmp_path / "ai_gateway"
    (brick / "skills" / "triage").mkdir(parents=True)
    (brick / "skills" / "billing").mkdir()
    (brick / "config.yml").write_text("skills:\n  schema: ${var.catalog}.skills\n", encoding="utf-8")
    return tmp_path


def _spec(skills: dict[str, Any]) -> AgentPayloadSpec:
    return AgentPayloadSpec.model_validate({"runtime": {"tools": {"skills": skills}}})


def test_workspace_source_reaches_no_brick_skill(instance_dir: Path) -> None:
    assert unreachable_brick_skills(instance_dir, _spec({}), VARS) == ["main.skills.billing", "main.skills.triage"]


def test_pinned_skill_is_reachable(instance_dir: Path) -> None:
    spec = _spec({"source": "unity_gateway", "gateway": {"pinned": [{"fqn": "main.skills.triage", "identity": "sp"}]}})
    assert unreachable_brick_skills(instance_dir, spec, VARS) == ["main.skills.billing"]


@pytest.mark.parametrize("block", ["discovery", "user_catalog"])
def test_enabled_location_reaches_the_whole_schema(instance_dir: Path, block: str) -> None:
    gateway = {block: {"enabled": True, "locations": ["main.skills"]}}
    spec = _spec({"source": "unity_gateway", "gateway": gateway})
    assert unreachable_brick_skills(instance_dir, spec, VARS) == []


def test_user_catalog_next_to_repo_skills_reaches_the_schema(instance_dir: Path) -> None:
    spec = _spec({"gateway": {"user_catalog": {"enabled": True, "locations": ["main.skills"]}}})
    assert unreachable_brick_skills(instance_dir, spec, VARS) == []


def test_no_brick_config_means_nothing_to_report(tmp_path: Path) -> None:
    assert unreachable_brick_skills(tmp_path, _spec({}), VARS) == []
