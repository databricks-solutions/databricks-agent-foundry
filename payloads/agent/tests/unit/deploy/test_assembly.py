"""Tests for the agent payload's provision manifest (issue #338).

Covers two layers:

* the :data:`PROVISION_STEPS` manifest itself -- order, protocol
  conformance, and the reverse-on-destroy / fail-fast semantics the
  :class:`AgentPayloadAdapter` relies on (exercised with recording fake
  steps so the contract is asserted without real Lakebase / SDK calls);
* the concrete :class:`SkillsStep` -- self-gating, validation-blocks,
  and the on-disk ``_assembled/`` artefact contract folded over from
  the former ``SkillsBrick``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_foundry._instance_layout import payload_root, resolve_payload_subdir
from agent_foundry.bricks import (
    BrickResult,
    DeployContext,
    DestroyContext,
    DestroyPlanItem,
)
from agent_foundry.contracts.skills import render_skill_md
from payloads.agent.adapter import AgentPayloadAdapter
from payloads.agent.deploy.assembly import PROVISION_STEPS, ProvisionStep
from payloads.agent.deploy.skills import SkillsStep

# ---------------------------------------------------------------------------
# Manifest shape
# ---------------------------------------------------------------------------


class TestProvisionManifest:
    def test_order_is_skills_mcp_lakebase(self) -> None:
        # MCP must sit before lakebase / the App render so its
        # ``mcp__<sub>`` variables are published in time (issue #338).
        assert [step.name for step in PROVISION_STEPS] == ["skills", "mcp", "lakebase"]

    def test_every_step_conforms_to_protocol(self) -> None:
        for step in PROVISION_STEPS:
            assert isinstance(step, ProvisionStep)


# ---------------------------------------------------------------------------
# Adapter manifest iteration (recording fakes)
# ---------------------------------------------------------------------------


class _RecordingStep:
    """A fake :class:`ProvisionStep` that records call order into *log*."""

    def __init__(self, name: str, log: list[str], *, rc: int = 0) -> None:
        self.name = name
        self._log = log
        self._rc = rc

    def provision(self, ctx: DeployContext) -> int:
        self._log.append(f"provision:{self.name}")
        ctx.variables[f"var__{self.name}"] = self.name
        return self._rc

    def plan_destroy(self, ctx: DestroyContext) -> list[DestroyPlanItem]:
        self._log.append(f"plan_destroy:{self.name}")
        return [DestroyPlanItem(brick="app", sub_instance=self.name, resource=self.name)]

    def destroy(self, ctx: DestroyContext) -> list[BrickResult]:
        self._log.append(f"destroy:{self.name}")
        return [BrickResult(name=self.name, status="ok", rc=0)]


def _patch_steps(monkeypatch: pytest.MonkeyPatch, steps: tuple[ProvisionStep, ...]) -> None:
    monkeypatch.setattr("payloads.agent.deploy.assembly.PROVISION_STEPS", steps)


class TestAdapterIteratesManifest:
    def test_provision_runs_in_order_and_merges_vars(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        log: list[str] = []
        _patch_steps(monkeypatch, (_RecordingStep("a", log), _RecordingStep("b", log)))
        ctx = DeployContext(instance_dir=tmp_path, target="dev")

        rc = AgentPayloadAdapter().provision(ctx)

        assert rc == 0
        assert log == ["provision:a", "provision:b"]
        # ``provision`` first seeds the agent's spec-derived variables
        # (llm endpoint, chat-history / file-upload toggles) into the
        # bag, then each step merges its own outputs in order.  Assert
        # the per-step vars are present (the merge contract) rather than
        # pinning the exact bag, which now also carries the spec vars.
        assert ctx.variables["var__a"] == "a"
        assert ctx.variables["var__b"] == "b"

    def test_provision_is_fail_fast(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        log: list[str] = []
        _patch_steps(monkeypatch, (_RecordingStep("a", log, rc=3), _RecordingStep("b", log)))
        ctx = DeployContext(instance_dir=tmp_path, target="dev")

        rc = AgentPayloadAdapter().provision(ctx)

        assert rc == 3
        assert log == ["provision:a"]  # "b" never runs

    def test_destroy_and_plan_destroy_reverse_order(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        log: list[str] = []
        _patch_steps(monkeypatch, (_RecordingStep("a", log), _RecordingStep("b", log)))
        ctx = DestroyContext(instance_dir=tmp_path, target="dev")

        plan = AgentPayloadAdapter().plan_destroy(ctx)
        results = AgentPayloadAdapter().destroy(ctx)

        assert log == ["plan_destroy:b", "plan_destroy:a", "destroy:b", "destroy:a"]
        assert [item.sub_instance for item in plan] == ["b", "a"]
        assert [r.name for r in results] == ["b", "a"]


# ---------------------------------------------------------------------------
# SkillsStep
# ---------------------------------------------------------------------------


def _write_valid_skill(skills_dir: Path, name: str) -> None:
    """Write a minimal *valid* ``<name>/SKILL.md`` skill under *skills_dir*."""
    skill = skills_dir / name
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(render_skill_md(name, "Test skill", "1. call test_tool"), encoding="utf-8")


@pytest.fixture
def agent_instance(tmp_path: Path) -> Path:
    """An agent-payload instance shell (config lives in ``payload.yml``).

    Skills are authored under the payload root
    (``payloads/agent/skills/``); ``SkillsStep`` resolves them via
    :func:`agent_foundry._instance_layout.resolve_payload_subdir`.
    """
    inst = tmp_path / "instances" / "test_app"
    agent_payload = payload_root(inst)
    agent_payload.mkdir(parents=True)
    (agent_payload / "payload.yml").write_text(
        'payload:\n  adapter: agent\n  spec:\n    runtime:\n      system_prompt: "You are a test."\n',
        encoding="utf-8",
    )
    return inst


def _skills_dir(instance_dir: Path) -> Path:
    """Payload-rooted ``skills/`` dir the assembler reads + writes."""
    return resolve_payload_subdir(instance_dir, "skills")


class TestSkillsStep:
    def test_noop_without_skills_dir(self, agent_instance: Path) -> None:
        ctx = DeployContext(instance_dir=agent_instance, target="dev")
        assert SkillsStep().provision(ctx) == 0
        assert not (_skills_dir(agent_instance) / "_assembled").exists()

    def test_assembles_artefacts(self, agent_instance: Path) -> None:
        _write_valid_skill(_skills_dir(agent_instance), "alpha")
        ctx = DeployContext(instance_dir=agent_instance, target="dev")

        assert SkillsStep().provision(ctx) == 0
        out = _skills_dir(agent_instance) / "_assembled"
        assert (out / "system_prompt.md").exists()
        tools = json.loads((out / "skill_tools.json").read_text(encoding="utf-8"))
        assert tools["skill_names"] == ["alpha"]
        # Eager mode (default) writes no routing index.
        assert not (out / "skill_index.json").exists()

    def test_eager_removes_a_stale_routing_index(self, agent_instance: Path) -> None:
        """Switching lazy -> eager must delete the index, not just skip it.

        The runtime treats the index's presence as the mode signal, so a
        leftover file re-attaches ``_load_skill`` on top of a prompt that
        already carries every skill body inline.
        """
        _write_valid_skill(_skills_dir(agent_instance), "alpha")
        out = _skills_dir(agent_instance) / "_assembled"
        out.mkdir(parents=True, exist_ok=True)
        stale = out / "skill_index.json"
        stale.write_text(
            json.dumps({"skill_loading": "lazy", "index": {"alpha": "alpha/SKILL.md"}}),
            encoding="utf-8",
        )

        ctx = DeployContext(instance_dir=agent_instance, target="dev")
        assert SkillsStep().provision(ctx) == 0

        assert not stale.exists()

    def test_blocks_on_validation_error(self, agent_instance: Path) -> None:
        # A SKILL.md without a description is a hard validation error
        # -> provision must fail-fast (return 1).
        broken = _skills_dir(agent_instance) / "broken"
        broken.mkdir(parents=True)
        (broken / "SKILL.md").write_text("---\nname: broken\n---\n\nbody\n", encoding="utf-8")
        ctx = DeployContext(instance_dir=agent_instance, target="dev")

        assert SkillsStep().provision(ctx) == 1

    def test_destroy_removes_assembled(self, agent_instance: Path) -> None:
        _write_valid_skill(_skills_dir(agent_instance), "alpha")
        ctx = DestroyContext(instance_dir=agent_instance, target="dev")
        SkillsStep().provision(DeployContext(instance_dir=agent_instance, target="dev"))

        plan = SkillsStep().plan_destroy(ctx)
        assert len(plan) == 1 and plan[0].destructive is False

        results = SkillsStep().destroy(ctx)
        assert results and results[0].status == "ok"
        assert not (_skills_dir(agent_instance) / "_assembled").exists()

    def test_plan_destroy_empty_without_assembled(self, agent_instance: Path) -> None:
        ctx = DestroyContext(instance_dir=agent_instance, target="dev")
        assert SkillsStep().plan_destroy(ctx) == []
        assert SkillsStep().destroy(ctx) == []
