"""Offline validation for the unity-gateway-assistant instance.

Run with:

    python -m pytest instances/<name>/tests/unit -v
"""

from __future__ import annotations

from pathlib import Path

from payloads.agent.config import load_agent_spec


class TestInstanceFoundryToml:
    def test_has_required_identity_fields(self, instance_config: dict) -> None:
        assert "name" in instance_config
        assert "schema_prefix" in instance_config

    def test_name_matches_directory(self, instance_dir: Path, instance_config: dict) -> None:
        norm = lambda s: s.replace("-", "_")  # noqa: E731
        assert norm(instance_config["name"]) == norm(instance_dir.name)


class TestGatewayPayload:
    def test_model_is_a_gateway_model_service(self, instance_dir: Path) -> None:
        model = load_agent_spec(instance_dir).runtime.model
        assert model is not None
        assert (model.kind, model.name) == ("model_service", "system.ai.claude-sonnet-5-5")

    def test_mcp_comes_from_the_gateway(self, instance_dir: Path) -> None:
        mcp = load_agent_spec(instance_dir).runtime.tools.mcp
        assert not mcp.source_is_deprecated()
        assert mcp.gateway is not None
        assert [(p.fqn, p.identity) for p in mcp.gateway.pinned] == [
            ("system.ai.web_search", "sp"),
            ("system.ai.genie_one_mcp", "obo"),
        ]

    def test_workspace_server_mounts_next_to_the_gateway(self, instance_dir: Path) -> None:
        mcp = load_agent_spec(instance_dir).runtime.tools.mcp
        assert (instance_dir / "payloads" / "agent" / "mcp_servers" / "sql_warehouse" / "server.yml").is_file()
        assert mcp.gateway is not None
        assert mcp.gateway.user_catalog.locations == ["system.ai"]

    def test_skills_use_the_new_form(self, instance_dir: Path) -> None:
        skills = load_agent_spec(instance_dir).runtime.tools.skills
        assert skills.deprecated_fields() == []
        assert not skills.repository_enabled()
        gateway = skills.effective_gateway()
        assert gateway is not None
        assert [p.identity for p in gateway.pinned] == ["sp"]

    def test_user_skills_live_in_lakebase(self, instance_dir: Path) -> None:
        skills = load_agent_spec(instance_dir).runtime.tools.skills
        assert skills.lakebase_user_skills() is not None
        gateway = skills.effective_gateway()
        assert gateway is not None
        assert not gateway.user_catalog.enabled

    def test_full_feature_set_is_on(self, instance_dir: Path) -> None:
        spec = load_agent_spec(instance_dir)
        context, ui = spec.runtime.context, spec.ui
        assert context.prompt_cache.enabled and context.tool_result_clearing.enabled
        assert spec.compaction_enabled() and spec.plan_enabled() and spec.memory_enabled()
        assert spec.runtime.reasoning.thinking == "adaptive"
        assert ui.file_upload.enabled and ui.file_upload.images
        assert ui.charts.enabled and ui.resources_sidebar.enabled

    def test_no_dormant_workspace_sources(self, instance_dir: Path) -> None:
        from payloads.agent.adapter import AgentPayloadAdapter

        assert AgentPayloadAdapter().validate_instance(instance_dir) == []


VARIABLES = {"catalog": "main", "schema_prefix": "unity_gateway_assistant"}


class TestGatewaySkill:
    def test_brick_skill_folder_is_valid(self, instance_dir: Path) -> None:
        from bricks.ai_gateway.config import skill_dirs
        from bricks.ai_gateway.skill_bundle import read_local_skill

        assert [read_local_skill(d).skill_id for d in skill_dirs(instance_dir)] == ["sql-runner"]

    def test_agent_pins_the_brick_skill(self, instance_dir: Path) -> None:
        from payloads.agent.deploy.gateway_reach import unreachable_brick_skills

        spec = load_agent_spec(instance_dir, variables=VARIABLES)
        assert unreachable_brick_skills(instance_dir, spec, VARIABLES) == []
