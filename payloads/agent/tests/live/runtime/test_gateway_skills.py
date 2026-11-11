"""Live checks for gateway skills against a real Unity Catalog skill.

Runs as the test principal, standing in for the App service principal.
Set ``AGENT_FOUNDRY_TEST_GATEWAY_SKILL`` to a skill FQN that principal can
read (``READ_VOLUME`` plus ``USE CATALOG`` / ``USE SCHEMA``).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from payloads.agent.contracts import GatewaySkillsRuntime
from payloads.agent.runtime.gateway_resolve import Candidate
from payloads.agent.runtime.gateway_skills import (
    GatewaySkillLoader,
    IndexedSkill,
    download_skill_md,
    ensure_baseline,
    fetch_entry,
    reset_baseline_cache,
)


def test_metadata_has_an_etag(serving_ws: Any, gateway_skill: str) -> None:
    entry = fetch_entry(serving_ws, Candidate(gateway_skill, "sp", "pinned"))
    assert entry.etag
    assert entry.name


def test_skill_md_downloads(serving_ws: Any, gateway_skill: str) -> None:
    assert download_skill_md(serving_ws, gateway_skill).strip()


@pytest.mark.asyncio
async def test_baseline_block_renders_the_skill(serving_ws: Any, gateway_skill: str) -> None:
    reset_baseline_cache()
    gateway = GatewaySkillsRuntime.model_validate({"pinned": [{"fqn": gateway_skill, "identity": "sp"}]})

    baseline = await ensure_baseline(gateway, serving_ws, with_bodies=True)

    assert baseline is not None
    assert f'fqn="{gateway_skill}"' in baseline.block
    reset_baseline_cache()


@pytest.mark.asyncio
async def test_loader_returns_the_runbook(serving_ws: Any, gateway_skill: str) -> None:
    entry = fetch_entry(serving_ws, Candidate(gateway_skill, "obo", "selected"))
    loader = GatewaySkillLoader({gateway_skill: IndexedSkill(entry, serving_ws, None)})

    response = await loader(None, json.dumps({"skill_id": gateway_skill}))

    assert response.startswith(f'<skill_runbook skill_id="{gateway_skill}"')
