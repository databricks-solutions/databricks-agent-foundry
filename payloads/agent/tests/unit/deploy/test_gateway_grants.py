"""App SP grants for ``sp``-pinned Unity Gateway MCP services."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from databricks.sdk.errors import PermissionDenied
from databricks.sdk.service.catalog import Privilege

from payloads.agent.config import resolve_agent_spec
from payloads.agent.contracts import AgentPayloadSpec, GatewayMcpRuntime, GatewaySkillsRuntime
from payloads.agent.deploy.config import agent_runtime_variables, gateway_user_scopes
from payloads.agent.deploy.gateway_grants import (
    GATEWAY_SP_MCP_SERVICES_VAR,
    GATEWAY_SP_SKILL_SCHEMAS_VAR,
    GATEWAY_SP_SKILLS_VAR,
    GatewayGrant,
    ensure_app_sp_gateway_grants,
    plan_gateway_grants,
    sp_discovery_schemas,
    sp_pinned_fqns,
    split_fqns,
)

SERVICES = ["main.tools.github", "main.tools.jira", "ops.mcp.pager"]


def _ws(sp_client_id: str | None = "sp-123") -> MagicMock:
    ws = MagicMock()
    ws.apps.get.return_value.service_principal_client_id = sp_client_id
    return ws


def _spec(tools: dict[str, Any]) -> AgentPayloadSpec:
    return AgentPayloadSpec.model_validate({"runtime": {"tools": tools}})


def _calls(ws: MagicMock) -> list[tuple[str, str, Privilege]]:
    return [
        (c.kwargs["securable_type"], c.kwargs["full_name"], c.kwargs["changes"][0].add[0])
        for c in ws.grants.update.call_args_list
    ]


GATEWAY_TOOLS = {
    "mcp": {
        "source": "unity_gateway",
        "gateway": {
            "pinned": [
                {"fqn": "main.tools.jira", "identity": "sp"},
                {"fqn": "main.tools.github", "identity": "sp"},
                {"fqn": "main.tools.personal", "identity": "obo"},
            ]
        },
    }
}


SKILL_GATEWAY: dict[str, Any] = {
    "pinned": [
        {"fqn": "main.skills.triage", "identity": "sp"},
        {"fqn": "main.skills.mine", "identity": "obo"},
    ],
    "discovery": {"enabled": True, "locations": ["ops.shared"]},
}


class TestPlan:
    def test_parents_then_services_deduplicated(self) -> None:
        assert plan_gateway_grants([*SERVICES, "main.tools.github"]) == [
            GatewayGrant("catalog", "main", Privilege.USE_CATALOG),
            GatewayGrant("catalog", "ops", Privilege.USE_CATALOG),
            GatewayGrant("schema", "main.tools", Privilege.USE_SCHEMA),
            GatewayGrant("schema", "ops.mcp", Privilege.USE_SCHEMA),
            GatewayGrant("mcp_service", "main.tools.github", Privilege.EXECUTE),
            GatewayGrant("mcp_service", "main.tools.jira", Privilege.EXECUTE),
            GatewayGrant("mcp_service", "ops.mcp.pager", Privilege.EXECUTE),
        ]

    def test_only_sp_pinned_services(self) -> None:
        gateway = GatewayMcpRuntime.model_validate(GATEWAY_TOOLS["mcp"]["gateway"])
        assert sp_pinned_fqns(gateway) == ["main.tools.github", "main.tools.jira"]

    def test_no_gateway_no_services(self) -> None:
        assert sp_pinned_fqns(None) == []

    def test_skills_get_read_volume_on_skill_and_discovery_schema(self) -> None:
        assert plan_gateway_grants([], skills=["main.skills.triage"], skill_schemas=["ops.shared"]) == [
            GatewayGrant("catalog", "main", Privilege.USE_CATALOG),
            GatewayGrant("catalog", "ops", Privilege.USE_CATALOG),
            GatewayGrant("schema", "main.skills", Privilege.USE_SCHEMA),
            GatewayGrant("schema", "ops.shared", Privilege.USE_SCHEMA),
            GatewayGrant("skill", "main.skills.triage", Privilege.READ_VOLUME),
            GatewayGrant("schema", "ops.shared", Privilege.READ_VOLUME),
        ]

    def test_skill_sources_are_sp_pins_and_enabled_discovery(self) -> None:
        gateway = GatewaySkillsRuntime.model_validate(SKILL_GATEWAY)
        assert sp_pinned_fqns(gateway) == ["main.skills.triage"]
        assert sp_discovery_schemas(gateway) == ["ops.shared"]
        disabled = GatewaySkillsRuntime.model_validate({**SKILL_GATEWAY, "discovery": {"locations": ["ops.shared"]}})
        assert sp_discovery_schemas(disabled) == []


class TestEnsure:
    def test_grants_to_app_sp(self) -> None:
        ws = _ws()

        ensure_app_sp_gateway_grants(ws, "my-app", ["main.tools.github"])

        assert _calls(ws) == [
            ("catalog", "main", Privilege.USE_CATALOG),
            ("schema", "main.tools", Privilege.USE_SCHEMA),
            ("mcp_service", "main.tools.github", Privilege.EXECUTE),
        ]
        assert {c.kwargs["changes"][0].principal for c in ws.grants.update.call_args_list} == {"sp-123"}

    def test_idempotent(self) -> None:
        ws = _ws()

        ensure_app_sp_gateway_grants(ws, "my-app", SERVICES)
        first = _calls(ws)
        ws.grants.update.reset_mock()
        ensure_app_sp_gateway_grants(ws, "my-app", SERVICES)

        assert _calls(ws) == first

    def test_permission_denied_reported_and_skipped(self, capsys: pytest.CaptureFixture[str]) -> None:
        ws = _ws()
        ws.grants.update.side_effect = [PermissionDenied("no"), None, None]

        ensure_app_sp_gateway_grants(ws, "my-app", ["main.tools.github"])

        assert ws.grants.update.call_count == 3
        assert "WARNING: could not grant USE_CATALOG on catalog main" in capsys.readouterr().out

    def test_warning_omits_sdk_config_dump(self, capsys: pytest.CaptureFixture[str]) -> None:
        ws = _ws()
        ws.grants.update.side_effect = PermissionDenied(
            "User does not have MANAGE on Catalog 'main'. Config: host=https://h, client_id=abc, auth_type=oauth-m2m"
        )

        ensure_app_sp_gateway_grants(ws, "my-app", ["main.tools.github"])

        out = capsys.readouterr().out
        assert "User does not have MANAGE on Catalog 'main'" in out
        assert "client_id" not in out

    def test_platform_services_skipped(self, capsys: pytest.CaptureFixture[str]) -> None:
        ws = _ws()

        ensure_app_sp_gateway_grants(ws, "my-app", ["system.ai.web_search", "main.tools.github"])

        assert all(not name.startswith("system") for _, name, _ in _calls(ws))
        assert "platform-managed Unity Gateway resources: system.ai.web_search" in capsys.readouterr().out

    def test_only_platform_services_no_calls(self) -> None:
        ws = _ws()
        ensure_app_sp_gateway_grants(ws, "my-app", ["system.ai.web_search"])
        ws.apps.get.assert_not_called()

    def test_nothing_pinned_no_calls(self) -> None:
        ws = _ws()
        ensure_app_sp_gateway_grants(ws, "my-app", [])
        ws.apps.get.assert_not_called()

    def test_dry_run_no_calls(self) -> None:
        ws = _ws()
        ensure_app_sp_gateway_grants(ws, "my-app", SERVICES, dry_run=True)
        ws.grants.update.assert_not_called()

    def test_missing_sp_skips(self) -> None:
        ws = _ws(sp_client_id=None)
        ensure_app_sp_gateway_grants(ws, "my-app", SERVICES)
        ws.grants.update.assert_not_called()


class TestDeployWiring:
    def test_runtime_variable_round_trips(self) -> None:
        variables = agent_runtime_variables(_spec(GATEWAY_TOOLS), {})
        assert split_fqns(variables[GATEWAY_SP_MCP_SERVICES_VAR]) == ["main.tools.github", "main.tools.jira"]

    def test_runtime_variable_publishes_resolved_placeholders(self) -> None:
        tools = {
            "mcp": {
                "source": "unity_gateway",
                "gateway": {"pinned": [{"fqn": "${var.catalog}.tools.github", "identity": "sp"}]},
            }
        }
        spec = resolve_agent_spec(None, {"runtime": {"tools": tools}}, variables={"catalog": "main"})
        variables = agent_runtime_variables(spec, {})
        assert split_fqns(variables[GATEWAY_SP_MCP_SERVICES_VAR]) == ["main.tools.github"]

    def test_runtime_variables_publish_sp_skills(self) -> None:
        tools = {"skills": {"source": "unity_gateway", "gateway": SKILL_GATEWAY}}
        variables = agent_runtime_variables(_spec(tools), {})
        assert variables[GATEWAY_SP_SKILLS_VAR] == "main.skills.triage"
        assert variables[GATEWAY_SP_SKILL_SCHEMAS_VAR] == "ops.shared"

    def test_runtime_variable_empty_for_workspace_source(self) -> None:
        assert agent_runtime_variables(_spec({}), {})[GATEWAY_SP_MCP_SERVICES_VAR] == ""

    def test_gateway_scope_added_once(self) -> None:
        spec = _spec(GATEWAY_TOOLS)
        assert gateway_user_scopes(["sql"], spec) == ["sql", "ai-gateway"]
        assert gateway_user_scopes(["ai-gateway"], spec) == ["ai-gateway"]

    def test_mcp_gateway_block_alone_adds_scope(self) -> None:
        assert gateway_user_scopes(["sql"], _spec({"mcp": {"gateway": {}}})) == ["sql", "ai-gateway"]

    def test_skills_user_catalog_next_to_repo_skills_adds_scope(self) -> None:
        skills = {"gateway": {"user_catalog": {"enabled": True, "locations": ["team.skills"]}}}
        assert gateway_user_scopes(["sql"], _spec({"skills": skills})) == ["sql", "ai-gateway"]

    def test_workspace_source_scopes_unchanged(self) -> None:
        assert gateway_user_scopes(["sql"], _spec({})) == ["sql"]

    def test_after_reconcile_grants_published_services(self) -> None:
        from payloads.agent.adapter import AgentPayloadAdapter

        ws = _ws()
        variables = {GATEWAY_SP_MCP_SERVICES_VAR: "main.tools.github", "chat_history_enabled": "false"}

        AgentPayloadAdapter().after_reconcile(ws, "my-app", variables, dry_run=False)

        assert ("mcp_service", "main.tools.github", Privilege.EXECUTE) in _calls(ws)

    def test_after_reconcile_grants_published_skills(self) -> None:
        from payloads.agent.adapter import AgentPayloadAdapter

        ws = _ws()
        variables = {
            GATEWAY_SP_SKILLS_VAR: "main.skills.triage",
            GATEWAY_SP_SKILL_SCHEMAS_VAR: "ops.shared",
            "chat_history_enabled": "false",
        }

        AgentPayloadAdapter().after_reconcile(ws, "my-app", variables, dry_run=False)

        assert ("skill", "main.skills.triage", Privilege.READ_VOLUME) in _calls(ws)
        assert ("schema", "ops.shared", Privilege.READ_VOLUME) in _calls(ws)
