"""Tests for the tool-source switches on the deploy path.

With ``skills.repository.enabled: false`` (or the deprecated
``source: unity_gateway``) the ``skills/`` tree is never read, so leftover
content there fails validation and the local skills step is skipped.
Repository and Unity Catalog skills may run together, but not under one
name.  MCP is presence-based: ``mcp_servers/`` and a gateway block mount
side by side.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from agent_foundry._instance_layout import payload_root
from agent_foundry.bricks import DeployContext
from agent_foundry.validate import check_instance_payloads
from payloads.agent.adapter import AgentPayloadAdapter
from payloads.agent.contracts import AgentPayloadSpec
from payloads.agent.deploy.assembly import active_provision_steps
from payloads.agent.deploy.sources import (
    dormant_source_dirs,
    duplicate_skill_names,
    warn_deprecated_mcp_source,
    warn_deprecated_skills_fields,
)

_GATEWAY_TOOLS = {"mcp": {"source": "unity_gateway"}, "skills": {"source": "unity_gateway"}}
_TRIAGE_PIN = {"fqn": "team.skills.triage", "identity": "sp"}
_COMBINED_SKILLS = {"skills": {"repository": {}, "unity_catalog": {"pinned": [_TRIAGE_PIN]}}}


def _write_payload(instance_dir: Path, tools: dict[str, Any]) -> AgentPayloadSpec:
    spec = {"runtime": {"tools": tools}}
    root = payload_root(instance_dir, "agent")
    root.mkdir(parents=True, exist_ok=True)
    (root / "payload.yml").write_text(yaml.dump({"payload": {"adapter": "agent", "spec": spec}}), encoding="utf-8")
    return AgentPayloadSpec.model_validate(spec)


def _add_skill(instance_dir: Path, name: str = "sales") -> Path:
    skill = payload_root(instance_dir, "agent") / "skills" / name
    skill.mkdir(parents=True)
    return skill


def _add_server_yml(instance_dir: Path, name: str = "github") -> Path:
    server = payload_root(instance_dir, "agent") / "mcp_servers" / name
    server.mkdir(parents=True)
    path = server / "server.yml"
    path.write_text("name: github\n", encoding="utf-8")
    return path


def _fail_if_called(spec: AgentPayloadSpec) -> tuple[()]:
    raise AssertionError("no provision step may run with dormant source dirs")


class TestDormantSourceDirs:
    @pytest.mark.parametrize("skills", [{"source": "unity_gateway"}, {"repository": {"enabled": False}}])
    def test_repository_off_with_skill_dir_names_it(self, tmp_path: Path, skills: dict[str, Any]) -> None:
        spec = _write_payload(tmp_path, {"skills": skills})
        skill = _add_skill(tmp_path)
        errors = dormant_source_dirs(tmp_path, spec)
        assert len(errors) == 1
        assert str(skill) in errors[0]
        assert "repository skills are off" in errors[0]

    def test_unity_catalog_next_to_repository_keeps_skill_dir(self, tmp_path: Path) -> None:
        spec = _write_payload(tmp_path, _COMBINED_SKILLS)
        _add_skill(tmp_path)
        assert dormant_source_dirs(tmp_path, spec) == []

    def test_gateway_mcp_keeps_server_yml(self, tmp_path: Path) -> None:
        spec = _write_payload(tmp_path, {"mcp": {"gateway": {}}})
        _add_server_yml(tmp_path)
        assert dormant_source_dirs(tmp_path, spec) == []

    def test_build_output_is_not_authored_content(self, tmp_path: Path) -> None:
        spec = _write_payload(tmp_path, _GATEWAY_TOOLS)
        _add_skill(tmp_path, "_assembled")
        assert dormant_source_dirs(tmp_path, spec) == []

    def test_default_sources_keep_local_trees(self, tmp_path: Path) -> None:
        spec = _write_payload(tmp_path, {})
        _add_skill(tmp_path)
        _add_server_yml(tmp_path)
        assert dormant_source_dirs(tmp_path, spec) == []


class TestDuplicateSkillNames:
    def test_repo_skill_named_like_a_pinned_skill_is_rejected(self, tmp_path: Path) -> None:
        spec = _write_payload(tmp_path, _COMBINED_SKILLS)
        skill = _add_skill(tmp_path, "triage")
        errors = duplicate_skill_names(tmp_path, spec)
        assert len(errors) == 1
        assert str(skill) in errors[0] and "team.skills.triage" in errors[0]

    def test_distinct_names_pass(self, tmp_path: Path) -> None:
        spec = _write_payload(tmp_path, _COMBINED_SKILLS)
        _add_skill(tmp_path, "sales")
        assert duplicate_skill_names(tmp_path, spec) == []

    def test_framework_validate_reports_duplicates(self, tmp_path: Path) -> None:
        _write_payload(tmp_path, _COMBINED_SKILLS)
        _add_skill(tmp_path, "triage")
        results = {r.name: r for r in check_instance_payloads(tmp_path)}
        assert results["Payload: agent"].passed is False
        assert "team.skills.triage" in results["Payload: agent"].detail


class TestValidateHook:
    def test_framework_validate_reports_dormant_dirs(self, tmp_path: Path) -> None:
        _write_payload(tmp_path, _GATEWAY_TOOLS)
        skill = _add_skill(tmp_path)
        results = {r.name: r for r in check_instance_payloads(tmp_path)}
        assert results["Payload: agent"].passed is False
        assert str(skill) in results["Payload: agent"].detail

    def test_framework_validate_passes_clean_gateway_instance(self, tmp_path: Path) -> None:
        _write_payload(tmp_path, _GATEWAY_TOOLS)
        results = {r.name: r for r in check_instance_payloads(tmp_path)}
        assert results["Payload: agent"].passed is True

    def test_provision_fails_before_any_step(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_payload(tmp_path, _GATEWAY_TOOLS)
        _add_skill(tmp_path)
        monkeypatch.setattr("payloads.agent.deploy.assembly.active_provision_steps", _fail_if_called)
        ctx = DeployContext(instance_dir=tmp_path, target="dev", variables={})
        assert AgentPayloadAdapter().provision(ctx) == 1


class TestActiveProvisionSteps:
    def test_default_mode_runs_every_step(self) -> None:
        names = [step.name for step in active_provision_steps(AgentPayloadSpec())]
        assert names == ["skills", "mcp", "lakebase"]

    @pytest.mark.parametrize(
        ("tools", "expected"),
        [
            ({"skills": {"source": "unity_gateway"}}, ["mcp", "lakebase"]),
            ({"skills": {"repository": {"enabled": False}}}, ["mcp", "lakebase"]),
            ({"mcp": {"gateway": {}}}, ["skills", "mcp", "lakebase"]),
            (_COMBINED_SKILLS, ["skills", "mcp", "lakebase"]),
            (_GATEWAY_TOOLS, ["mcp", "lakebase"]),
        ],
    )
    def test_only_disabled_repository_skips_its_local_step(self, tools: dict[str, Any], expected: list[str]) -> None:
        spec = AgentPayloadSpec.model_validate({"runtime": {"tools": tools}})
        assert [step.name for step in active_provision_steps(spec)] == expected


class TestDeprecatedMcpSource:
    def test_explicit_source_warns(self, capsys: pytest.CaptureFixture[str]) -> None:
        warn_deprecated_mcp_source(_mcp_spec({"source": "workspace"}))
        assert "mcp.source is deprecated" in capsys.readouterr().out

    def test_presence_based_spec_is_silent(self, capsys: pytest.CaptureFixture[str]) -> None:
        warn_deprecated_mcp_source(_mcp_spec({"gateway": {}}))
        assert capsys.readouterr().out == ""


class TestDeprecatedSkillsFields:
    def test_legacy_fields_warn(self, capsys: pytest.CaptureFixture[str]) -> None:
        spec = _skills_spec({"source": "unity_gateway", "gateway": {}})
        warn_deprecated_skills_fields(spec)
        assert "skills source, gateway is deprecated" in capsys.readouterr().out

    def test_new_form_is_silent(self, capsys: pytest.CaptureFixture[str]) -> None:
        warn_deprecated_skills_fields(_skills_spec(_COMBINED_SKILLS["skills"]))
        assert capsys.readouterr().out == ""


def _skills_spec(skills: dict[str, Any]) -> AgentPayloadSpec:
    return AgentPayloadSpec.model_validate({"runtime": {"tools": {"skills": skills}}})


def _mcp_spec(mcp: dict[str, Any]) -> AgentPayloadSpec:
    return AgentPayloadSpec.model_validate({"runtime": {"tools": {"mcp": mcp}}})
