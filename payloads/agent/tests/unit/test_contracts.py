"""Tests for the agent payload's typed ``spec`` contract.

These contracts are **payload-owned** (``payloads.agent.contracts``); the
framework knows only the generic ``PayloadFile`` envelope.  The surface is
a hard-cut 2.0 contract: every spec model is ``extra="forbid"`` so a
misspelled or misplaced key fails fast.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import BaseModel, ValidationError

from payloads.agent.contracts import (
    DEFAULT_SKILL_ROUTING_HEADER,
    AgentPayloadFile,
    AgentPayloadSpec,
    AgentSkillsRuntime,
    AgentToolsRuntime,
    ChatHistoryUI,
    DiscoverySpec,
    GatewayMcpRuntime,
    GatewaySkillsRuntime,
    McpDiscoverySpec,
    McpLimits,
    ModelServiceModel,
    PinnedMcp,
    PinnedSkill,
    ServingEndpointModel,
    SkillDiscoverySpec,
    SkillLimits,
    SkillLoadingMode,
    effective_skill_loading,
)


def _uc_tool_dict(name: str = "lookup") -> dict[str, Any]:
    return {
        "name": name,
        "description": "desc",
        "type": "uc_function",
        "function_name": "cat.sch.fn",
    }


class TestAgentPayloadSpecDefaults:
    def test_empty_spec_is_fully_defaulted(self) -> None:
        spec = AgentPayloadSpec()
        # runtime
        assert spec.runtime.model is None
        assert spec.runtime.tools.mcp_only is None
        assert spec.runtime.tools.skills.loading is None
        # ui
        assert spec.ui.enabled is True
        assert spec.ui.chat_history.enabled is True
        assert spec.ui.file_upload.enabled is False
        assert spec.ui.file_upload.max_mb == 10
        assert spec.ui.feedback.enabled is True
        assert spec.ui.charts.enabled is True
        assert spec.ui.reports.enabled is False
        # Tool panels start collapsed unless a payload opts in.
        assert spec.ui.tool_output.expanded is False
        # integrations
        assert spec.integrations.a2a.enabled is False
        assert spec.integrations.mcp_server.enabled is False

    def test_default_routing_header_constant_exists(self) -> None:
        assert "_load_skill" in DEFAULT_SKILL_ROUTING_HEADER


class TestChatHistoryDays:
    def test_defaults(self) -> None:
        history = ChatHistoryUI()
        assert history.sidebar_days == 30
        assert history.retention_days == 365

    def test_null_turns_either_off(self) -> None:
        history = ChatHistoryUI(sidebar_days=None, retention_days=None)
        assert history.sidebar_days is None
        assert history.retention_days is None

    def test_retention_shorter_than_sidebar_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="retention_days"):
            ChatHistoryUI(sidebar_days=30, retention_days=7)

    def test_retention_may_be_set_without_sidebar(self) -> None:
        assert ChatHistoryUI(sidebar_days=None, retention_days=7).retention_days == 7

    @pytest.mark.parametrize("field", ["sidebar_days", "retention_days"])
    def test_zero_days_is_rejected(self, field: str) -> None:
        with pytest.raises(ValidationError):
            ChatHistoryUI.model_validate({field: 0})


def _user_skills_spec(**overrides: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "runtime": {"tools": {"skills": {"user_skills": {"store": "lakebase"}}}},
        "ui": {"chat_history": {"enabled": True}, "resources_sidebar": {"enabled": True}},
    }
    spec.update(overrides)
    return spec


class TestUserSkills:
    def test_off_by_default(self) -> None:
        skills = AgentPayloadSpec().runtime.tools.skills
        assert not skills.user_skills.is_enabled()
        assert skills.lakebase_user_skills() is None

    def test_lakebase_with_history_and_panel_validates(self) -> None:
        spec = AgentPayloadSpec.model_validate(_user_skills_spec())
        user_skills = spec.runtime.tools.skills.user_skills
        assert (user_skills.store, user_skills.max_skills, user_skills.max_skill_md_chars) == ("lakebase", 20, 20_000)

    def test_allowed_with_unity_gateway_skills(self) -> None:
        tools = {"skills": {"source": "unity_gateway", "user_skills": {"store": "lakebase"}}}
        spec = AgentPayloadSpec.model_validate(_user_skills_spec(runtime={"tools": tools}))
        assert spec.runtime.tools.skills.user_skills.store == "lakebase"

    @pytest.mark.parametrize("source", ["foundry", "unity_gateway"])
    def test_rejected_next_to_the_gateway_user_catalog(self, source: str) -> None:
        gateway = {"user_catalog": {"enabled": True, "locations": ["team.skills"]}}
        tools = {"skills": {"source": source, "gateway": gateway, "user_skills": {"store": "lakebase"}}}
        with pytest.raises(ValidationError, match="pick one user skill store"):
            AgentPayloadSpec.model_validate(_user_skills_spec(runtime={"tools": tools}))

    @pytest.mark.parametrize("section", ["chat_history", "resources_sidebar"])
    def test_requires_history_and_panel(self, section: str) -> None:
        ui = {"chat_history": {"enabled": True}, "resources_sidebar": {"enabled": True}, section: {"enabled": False}}
        with pytest.raises(ValidationError, match=section):
            AgentPayloadSpec.model_validate(_user_skills_spec(ui=ui))


class TestExtraForbid:
    def test_misspelled_runtime_key_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AgentPayloadSpec.model_validate({"runtime": {"llm_endpiont_name": "x"}})

    def test_misplaced_ui_toggle_rejected(self) -> None:
        # ``charts`` is a ui feature, not a top-level spec key.
        with pytest.raises(ValidationError):
            AgentPayloadSpec.model_validate({"charts": {"enabled": True}})

    def test_skills_loading_only_allows_known_modes(self) -> None:
        with pytest.raises(ValidationError):
            AgentPayloadSpec.model_validate({"runtime": {"tools": {"skills": {"loading": "turbo"}}}})

    def test_chat_layout_width_defaults_to_4xl(self) -> None:
        assert AgentPayloadSpec().ui.chat_layout.width == "4xl"

    @pytest.mark.parametrize("width", ["3xl", "8xl", "full", "6XL", ""])
    def test_chat_layout_width_rejects_unknown_steps(self, width: str) -> None:
        with pytest.raises(ValidationError):
            AgentPayloadSpec.model_validate({"ui": {"chat_layout": {"width": width}}})

    def test_chat_layout_rejects_unknown_key(self) -> None:
        with pytest.raises(ValidationError):
            AgentPayloadSpec.model_validate({"ui": {"chat_layout": {"enabled": True}}})

    def test_tool_output_rejects_enabled_key(self) -> None:
        # ``tool_output`` is a presentation mode keyed by ``expanded``, not
        # a mount toggle -- ``enabled`` must not silently no-op.
        with pytest.raises(ValidationError):
            AgentPayloadSpec.model_validate({"ui": {"tool_output": {"enabled": True}}})


class TestToolsDefinitions:
    def test_list_shape_accepted(self) -> None:
        tools = AgentToolsRuntime.model_validate({"definitions": [_uc_tool_dict()]})
        assert tools.definitions is not None
        assert tools.definitions[0].name == "lookup"

    def test_mapping_shape_rejected_with_migration_hint(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            AgentToolsRuntime.model_validate({"definitions": {"uc": {"catalog": "x"}}})
        assert "list of Tool" in str(exc_info.value)

    def test_empty_list_valid(self) -> None:
        tools = AgentToolsRuntime.model_validate({"definitions": []})
        assert tools.definitions == []


class TestPayloadEnvelope:
    def test_full_envelope_round_trips(self) -> None:
        f = AgentPayloadFile.model_validate(
            {
                "payload": {
                    "adapter": "agent",
                    "spec": {
                        "runtime": {"model": {"kind": "serving_endpoint", "name": "ep"}},
                        "ui": {"file_upload": {"enabled": True, "max_mb": 25}},
                        "integrations": {"a2a": {"enabled": True}},
                    },
                }
            }
        )
        assert f.payload.adapter == "agent"
        assert f.payload.spec.runtime.model is not None
        assert f.payload.spec.runtime.model.name == "ep"
        assert f.payload.spec.ui.file_upload.max_mb == 25
        assert f.payload.spec.integrations.a2a.enabled is True

    def test_missing_payload_block_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AgentPayloadFile.model_validate({})


_REPO_ROOT = Path(__file__).resolve().parents[4]


def _tools(**tools: Any) -> AgentPayloadSpec:
    return AgentPayloadSpec.model_validate({"runtime": {"tools": tools}})


class TestGatewayDefaults:
    def test_sources_default_to_local_trees(self) -> None:
        spec = AgentPayloadSpec()
        assert spec.runtime.tools.mcp.source == "workspace"
        assert spec.runtime.tools.mcp.gateway is None
        assert spec.runtime.tools.skills.repository_enabled()
        assert spec.runtime.tools.skills.effective_gateway() is None
        assert spec.runtime.model is None
        assert spec.ui.resources_sidebar.enabled is False

    def test_gateway_block_defaults(self) -> None:
        mcp = GatewayMcpRuntime()
        skills = GatewaySkillsRuntime()
        assert mcp.pinned == []
        assert mcp.discovery.enabled is False
        assert mcp.user_catalog.enabled is False
        assert mcp.user_catalog.limits == McpLimits(max_servers=8, max_tools=60)
        assert skills.user_catalog.limits == SkillLimits(max_skills=20, max_loaded_chars=40_000)
        assert skills.refresh_seconds == 900
        assert skills.authoring.enabled is False
        assert skills.authoring.max_skill_md_chars == 20_000

    def test_portfolio_example_parses_unchanged(self) -> None:
        path = _REPO_ROOT / "examples" / "portfolio_assistant" / "payloads" / "agent" / "payload.yml"
        payload = AgentPayloadFile.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        tools = payload.payload.spec.runtime.tools
        assert tools.mcp.source == "workspace"
        assert tools.skills.repository_enabled()


class TestModelUnion:
    @pytest.mark.parametrize(
        ("model", "cls"),
        [
            ({"kind": "serving_endpoint", "name": "databricks-claude-sonnet-4-5"}, ServingEndpointModel),
            ({"kind": "model_service", "name": "system.ai.claude-sonnet-4-5"}, ModelServiceModel),
        ],
    )
    def test_both_kinds_parse(self, model: dict[str, str], cls: type[BaseModel]) -> None:
        parsed = AgentPayloadSpec.model_validate({"runtime": {"model": model}}).runtime.model
        assert isinstance(parsed, cls)
        assert parsed is not None and parsed.name == model["name"]

    def test_unknown_kind_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AgentPayloadSpec.model_validate({"runtime": {"model": {"kind": "external", "name": "x"}}})

    def test_removed_llm_endpoint_name_alias_rejected(self) -> None:
        with pytest.raises(ValidationError, match="llm_endpoint_name"):
            AgentPayloadSpec.model_validate({"runtime": {"llm_endpoint_name": "ep"}})

    @pytest.mark.parametrize("name", ["claude", "ai.claude", "a.b.c.d", "a.b.c/../d", "a.b.c d"])
    def test_model_service_name_must_be_three_part_fqn(self, name: str) -> None:
        with pytest.raises(ValidationError):
            AgentPayloadSpec.model_validate({"runtime": {"model": {"kind": "model_service", "name": name}}})

    def test_model_owns_extra_body_model_key(self) -> None:
        with pytest.raises(ValidationError, match="spec.runtime.model"):
            AgentPayloadSpec.model_validate({"runtime": {"model_settings": {"extra_body": {"model": "x"}}}})


class TestDiscoverySpec:
    def test_enabled_requires_locations(self) -> None:
        with pytest.raises(ValidationError, match="location"):
            DiscoverySpec(enabled=True)

    @pytest.mark.parametrize("location", ["catalog", "a.b.c", "a.b;drop", ""])
    def test_locations_must_be_catalog_schema(self, location: str) -> None:
        with pytest.raises(ValidationError):
            DiscoverySpec(enabled=True, locations=[location])

    @pytest.mark.parametrize(
        "limits",
        [
            {"max_servers": 0},
            {"max_tools": -1},
        ],
    )
    def test_mcp_limits_must_be_positive(self, limits: dict[str, int]) -> None:
        with pytest.raises(ValidationError):
            McpDiscoverySpec.model_validate({"limits": limits})

    @pytest.mark.parametrize("limits", [{"max_skills": 0}, {"max_loaded_chars": 0}])
    def test_skill_limits_must_be_positive(self, limits: dict[str, int]) -> None:
        with pytest.raises(ValidationError):
            SkillDiscoverySpec.model_validate({"limits": limits})

    def test_unknown_key_rejected(self) -> None:
        with pytest.raises(ValidationError):
            McpDiscoverySpec.model_validate({"identity": "obo"})

    def test_skills_block_has_no_exclude_tools(self) -> None:
        with pytest.raises(ValidationError):
            SkillDiscoverySpec.model_validate({"exclude_tools": ["x"]})


class TestSourceSwitches:
    def test_mcp_gateway_block_alone_mounts_gateway(self) -> None:
        mcp = _tools(mcp={"gateway": {}}).runtime.tools.mcp
        assert mcp.effective_gateway() == GatewayMcpRuntime()
        assert mcp.source_is_deprecated() is False

    @pytest.mark.parametrize(
        ("mcp", "expected"),
        [
            ({}, None),
            ({"source": "workspace"}, None),
            ({"source": "unity_gateway"}, GatewayMcpRuntime()),
            ({"source": "workspace", "gateway": {"discovery": {}}}, GatewayMcpRuntime()),
        ],
    )
    def test_deprecated_mcp_source_maps_to_gateway_presence(
        self, mcp: dict[str, Any], expected: GatewayMcpRuntime | None
    ) -> None:
        assert _tools(mcp=mcp).runtime.tools.mcp.effective_gateway() == expected

    def test_explicit_mcp_source_is_deprecated(self) -> None:
        assert _tools(mcp={"source": "workspace"}).runtime.tools.mcp.source_is_deprecated() is True

    def test_skills_user_catalog_next_to_repo_skills(self) -> None:
        gateway = {"user_catalog": {"enabled": True, "locations": ["team.skills"]}}
        skills = _tools(skills={"gateway": gateway}).runtime.tools.skills
        assert skills.repository_enabled()
        assert skills.effective_gateway() == skills.gateway

    @pytest.mark.parametrize(
        "gateway",
        [
            {"pinned": [{"fqn": "a.b.c", "identity": "sp"}]},
            {"discovery": {"enabled": True, "locations": ["team.skills"]}},
            {
                "user_catalog": {"enabled": True, "locations": ["team.skills"]},
                "authoring": {"enabled": True, "schema": "team.skills"},
            },
        ],
    )
    def test_agent_skills_from_the_gateway_need_its_source(self, gateway: dict[str, Any]) -> None:
        with pytest.raises(ValidationError, match="allows only user_catalog"):
            _tools(skills={"gateway": gateway})

    @pytest.mark.parametrize(
        ("authored", "cached", "expected"),
        [(None, False, "eager"), ("lazy", False, "lazy"), ("lazy", True, "eager"), ("eager", True, "eager")],
    )
    def test_prompt_cache_overrides_lazy_loading(
        self, authored: SkillLoadingMode | None, cached: bool, expected: SkillLoadingMode
    ) -> None:
        assert effective_skill_loading(authored, prompt_cache_enabled=cached) == expected

    def test_gateway_sources_accept_gateway_blocks(self) -> None:
        spec = _tools(
            mcp={"source": "unity_gateway", "gateway": {"pinned": [{"fqn": "system.ai.dbsql", "identity": "sp"}]}},
            skills={"source": "unity_gateway", "gateway": {"pinned": [{"fqn": "a.b.c", "identity": "obo"}]}},
        )
        assert spec.runtime.tools.mcp.gateway is not None
        assert spec.runtime.tools.mcp.gateway.pinned[0].tools.allow == []
        assert spec.runtime.tools.skills.gateway is not None

    def test_unknown_source_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _tools(mcp={"source": "registry"})


class TestPinned:
    @pytest.mark.parametrize("cls", [PinnedMcp, PinnedSkill])
    def test_identity_required(self, cls: type[BaseModel]) -> None:
        with pytest.raises(ValidationError):
            cls.model_validate({"fqn": "a.b.c"})

    @pytest.mark.parametrize("cls", [PinnedMcp, PinnedSkill])
    @pytest.mark.parametrize("identity", ["auto", "user", "SP", ""])
    def test_only_obo_or_sp(self, cls: type[BaseModel], identity: str) -> None:
        with pytest.raises(ValidationError):
            cls.model_validate({"fqn": "a.b.c", "identity": identity})

    def test_duplicate_mcp_fqn_rejected(self) -> None:
        entry = {"fqn": "system.ai.dbsql", "identity": "sp"}
        with pytest.raises(ValidationError, match="more than once"):
            GatewayMcpRuntime.model_validate({"pinned": [entry, {**entry, "identity": "obo"}]})

    def test_duplicate_skill_fqn_rejected(self) -> None:
        entry = {"fqn": "a.b.c", "identity": "sp"}
        with pytest.raises(ValidationError, match="more than once"):
            GatewaySkillsRuntime.model_validate({"pinned": [entry, entry]})


class TestSkillAuthoring:
    def test_schema_must_be_a_user_catalog_location(self) -> None:
        with pytest.raises(ValidationError, match="user_catalog.locations"):
            GatewaySkillsRuntime.model_validate(
                {
                    "user_catalog": {"enabled": True, "locations": ["team.shared_skills"]},
                    "authoring": {"enabled": True, "schema": "team.user_skills"},
                }
            )

    def test_enabled_requires_schema(self) -> None:
        with pytest.raises(ValidationError, match="schema is required"):
            GatewaySkillsRuntime.model_validate({"authoring": {"enabled": True}})

    def test_schema_in_user_catalog_accepted(self) -> None:
        skills = GatewaySkillsRuntime.model_validate(
            {
                "user_catalog": {"enabled": True, "locations": ["team.user_skills"]},
                "authoring": {"enabled": True, "schema": "team.user_skills"},
            }
        )
        assert skills.authoring.schema_name == "team.user_skills"


_UC_PIN = {"fqn": "team.skills.triage", "identity": "sp"}
_UC_USER_STORE = {
    "enabled": True,
    "store": "unity_catalog",
    "locations": ["team.user_skills"],
    "include": ["*.u-*"],
    "authoring_schema": "team.user_skills",
    "max_skills": 5,
    "max_skill_md_chars": 1000,
}


def _skills(**skills: Any) -> AgentSkillsRuntime:
    return AgentSkillsRuntime.model_validate(skills)


class TestSkillsHierarchy:
    def test_toggle_defaults(self) -> None:
        skills = _skills(repository={}, unity_catalog={}, user_skills={"store": "lakebase", "enabled": True})
        assert skills.repository_enabled()
        assert skills.effective_gateway() == GatewaySkillsRuntime()
        assert skills.lakebase_user_skills() is skills.user_skills
        assert skills.deprecated_fields() == []

    def test_repository_and_unity_catalog_combine(self) -> None:
        skills = _skills(repository={"enabled": True}, unity_catalog={"pinned": [_UC_PIN], "refresh_seconds": 0})
        gateway = skills.effective_gateway()
        assert skills.repository_enabled()
        assert gateway is not None
        assert [p.fqn for p in gateway.pinned] == ["team.skills.triage"]
        assert gateway.refresh_seconds == 0
        assert not gateway.user_catalog.enabled

    def test_repository_off(self) -> None:
        assert not _skills(repository={"enabled": False}).repository_enabled()

    def test_unity_catalog_off_yields_no_gateway(self) -> None:
        assert _skills(unity_catalog={"enabled": False, "pinned": [_UC_PIN]}).effective_gateway() is None

    def test_disabled_unity_catalog_block_is_still_validated(self) -> None:
        with pytest.raises(ValidationError, match="more than once"):
            _skills(unity_catalog={"enabled": False, "pinned": [_UC_PIN, _UC_PIN]})

    def test_unity_catalog_user_store_maps_to_user_catalog_and_authoring(self) -> None:
        skills = _skills(user_skills=_UC_USER_STORE)
        gateway = skills.effective_gateway()
        assert gateway is not None
        assert (gateway.pinned, gateway.discovery.enabled) == ([], False)
        assert gateway.user_catalog.enabled
        assert gateway.user_catalog.locations == ["team.user_skills"]
        assert gateway.user_catalog.include == ["*.u-*"]
        assert gateway.user_catalog.limits.max_skills == 5
        assert (gateway.authoring.enabled, gateway.authoring.schema_name) == (True, "team.user_skills")
        assert gateway.authoring.max_skill_md_chars == 1000
        assert skills.lakebase_user_skills() is None

    def test_disabled_user_store_contributes_nothing(self) -> None:
        assert _skills(user_skills={**_UC_USER_STORE, "enabled": False}).effective_gateway() is None

    @pytest.mark.parametrize(
        ("user_skills", "match"),
        [
            ({"store": "lakebase", "locations": ["a.b"]}, "require store: unity_catalog"),
            ({"enabled": True, "store": "unity_catalog"}, "at least one"),
            ({**_UC_USER_STORE, "authoring_schema": "team.other"}, "must be one of its locations"),
        ],
    )
    def test_user_store_fields_are_checked(self, user_skills: dict[str, Any], match: str) -> None:
        with pytest.raises(ValidationError, match=match):
            _skills(user_skills=user_skills)

    @pytest.mark.parametrize(
        "skills",
        [
            {"source": "foundry", "repository": {}},
            {"source": "unity_gateway", "unity_catalog": {}},
            {"gateway": {}, "unity_catalog": {}},
        ],
    )
    def test_mixing_old_and_new_forms_is_rejected(self, skills: dict[str, Any]) -> None:
        with pytest.raises(ValidationError, match="not both"):
            _tools(skills=skills)


class TestLegacySkillsMapping:
    def test_source_unity_gateway_turns_repository_off(self) -> None:
        skills = _skills(source="unity_gateway")
        assert not skills.repository_enabled()
        assert skills.effective_gateway() == GatewaySkillsRuntime()
        assert skills.deprecated_fields() == ["source"]

    def test_source_foundry_keeps_repository_on(self) -> None:
        skills = _skills(source="foundry")
        assert skills.repository_enabled()
        assert skills.effective_gateway() is None
        assert skills.deprecated_fields() == ["source"]

    def test_gateway_block_is_read_as_is(self) -> None:
        skills = _skills(source="unity_gateway", gateway={"pinned": [_UC_PIN], "refresh_seconds": 60})
        assert skills.effective_gateway() == skills.gateway
        assert skills.deprecated_fields() == ["source", "gateway"]

    def test_store_without_enabled_turns_user_skills_on(self) -> None:
        skills = _skills(user_skills={"store": "lakebase"})
        assert skills.lakebase_user_skills() is skills.user_skills
        assert skills.deprecated_fields() == ["user_skills.store without user_skills.enabled"]
