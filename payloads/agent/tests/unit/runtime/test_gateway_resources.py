"""The chat sidebar's sections: agent resources, user catalog, limits, tools gate."""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from databricks.sdk.errors import NotFound, PermissionDenied
from databricks.sdk.service.catalog import McpService, Skill

from payloads.agent.contracts import GatewayMcpRuntime, GatewaySkillsRuntime, UserSkills
from payloads.agent.runtime import gateway_resources
from payloads.agent.runtime.gateway_resolve import Candidate, clear_listing_cache
from payloads.agent.runtime.gateway_resources import (
    ResourceNotAllowedError,
    UserTokenRequiredError,
    build_resources,
    list_mcp_tools,
    mcp_tools_candidate,
    user_catalog_limits,
)

DISCOVERY = {"enabled": True, "locations": ["team.mcp"]}
CATALOG = {"enabled": True, "locations": ["team.user_mcp"], "exclude": ["team.user_mcp.secret"]}


@pytest.fixture(autouse=True)
def _fresh_cache() -> Iterator[None]:
    clear_listing_cache()
    yield
    clear_listing_cache()


def _mcp_config(**blocks: Any) -> dict[str, Any]:
    return {"GATEWAY_MCP": GatewayMcpRuntime.model_validate(blocks)}


def _ws(name: str, *services: str) -> MagicMock:
    ws = MagicMock(name=name)
    ws.config.client_id = name
    ws.config.host = "https://example.cloud.databricks.com"
    ws.ai_gateway.list_mcp_services.return_value = [
        McpService(name=f"mcp-services/{fqn}", comment=f"about {fqn}") for fqn in services
    ]
    ws.ai_gateway.get_mcp_service.side_effect = lambda name: McpService(name=name, comment="described")
    return ws


def _server(list_tools: AsyncMock) -> MagicMock:
    server = MagicMock()
    server.__aenter__ = AsyncMock(return_value=server)
    server.__aexit__ = AsyncMock(return_value=False)
    server.list_tools = list_tools
    return server


class TestAgentSection:
    def test_pinned_and_discovered_carry_identity_and_details(self) -> None:
        config = _mcp_config(pinned=[{"fqn": "team.mcp.jira", "identity": "sp"}], discovery=DISCOVERY)
        sp_ws = _ws("sp", "team.mcp.kafka")

        response = build_resources(config, sp_ws, None)

        assert [(e.fqn, e.identity, e.origin, e.description) for e in response.agent] == [
            ("team.mcp.jira", "sp", "pinned", "described"),
            ("team.mcp.kafka", "sp", "discovered", "described"),
        ]

    def test_obo_pinned_without_user_token_is_flagged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DATABRICKS_APP_NAME", "test-app")
        config = _mcp_config(pinned=[{"fqn": "team.mcp.jira", "identity": "obo"}])

        (entry,) = build_resources(config, _ws("sp"), None).agent

        assert entry.reason == "no_user_token"
        assert entry.display_name == "jira"

    def test_unreadable_service_is_flagged_unavailable(self) -> None:
        config = _mcp_config(pinned=[{"fqn": "team.mcp.jira", "identity": "sp"}])
        sp_ws = _ws("sp")
        sp_ws.ai_gateway.get_mcp_service.side_effect = NotFound("gone")

        (entry,) = build_resources(config, sp_ws, None).agent

        assert entry.reason == "unavailable"

    def test_workspace_servers_and_foundry_skills_are_listed(self) -> None:
        config = {"REPOSITORY_SKILLS": True, "SKILL_NAMES": ["triage"]}

        response = build_resources(config, _ws("sp"), None, workspace_mcp_names=["github"])

        assert [(e.kind, e.fqn, e.source) for e in response.agent] == [
            ("mcp_service", "github", "workspace"),
            ("skill", "triage", "foundry"),
        ]
        assert response.user == []
        assert response.user_reason is None

    def test_repository_and_unity_catalog_skills_are_listed_together(self) -> None:
        skills = GatewaySkillsRuntime.model_validate({"pinned": [{"fqn": "team.skills.sql-runner", "identity": "sp"}]})
        config = {"REPOSITORY_SKILLS": True, "SKILL_NAMES": ["triage"], "GATEWAY_SKILLS": skills}

        response = build_resources(config, _ws("sp"), None)

        assert [(e.kind, e.fqn, e.source) for e in response.agent] == [
            ("skill", "team.skills.sql-runner", "unity_gateway"),
            ("skill", "triage", "foundry"),
        ]

    def test_repository_off_hides_repo_skills(self) -> None:
        response = build_resources({"REPOSITORY_SKILLS": False, "SKILL_NAMES": ["triage"]}, _ws("sp"), None)

        assert response.agent == []


class TestUserSection:
    def test_lists_as_user_filtered_by_policy_and_agent_set(self) -> None:
        config = _mcp_config(pinned=[{"fqn": "team.user_mcp.jira", "identity": "sp"}], user_catalog=CATALOG)
        user_ws = _ws("user", "team.user_mcp.jira", "team.user_mcp.gmail", "team.user_mcp.secret")

        response = build_resources(config, _ws("sp"), user_ws)

        assert [(e.fqn, e.identity, e.origin, e.description) for e in response.user] == [
            ("team.user_mcp.gmail", "obo", "user_catalog", "about team.user_mcp.gmail"),
        ]
        user_ws.ai_gateway.list_mcp_services.assert_called_once_with(parent="schemas/team.user_mcp")

    def test_without_user_token_reports_reason(self) -> None:
        response = build_resources(_mcp_config(user_catalog=CATALOG), _ws("sp"), None)

        assert response.user == []
        assert response.user_reason == "no_user_token"

    def test_listing_failure_reports_reason(self) -> None:
        user_ws = _ws("user")
        user_ws.ai_gateway.list_mcp_services.side_effect = PermissionDenied("no")

        response = build_resources(_mcp_config(user_catalog=CATALOG), _ws("sp"), user_ws)

        assert response.user_reason == "listing_failed"

    def test_lists_skills_with_bundle_name(self) -> None:
        skills = GatewaySkillsRuntime.model_validate({"user_catalog": {"enabled": True, "locations": ["team.skills"]}})
        user_ws = _ws("user")
        user_ws.ai_gateway.list_skills.return_value = [
            Skill(name="skills/team.skills.triage", bundle_name="Triage", description="Sort tickets"),
        ]

        response = build_resources({"REPOSITORY_SKILLS": False, "GATEWAY_SKILLS": skills}, _ws("sp"), user_ws)

        assert [(e.kind, e.display_name, e.description) for e in response.user] == [("skill", "Triage", "Sort tickets")]


class TestLimits:
    def test_only_enabled_catalogs_publish_limits(self) -> None:
        limits = user_catalog_limits(_mcp_config(user_catalog=CATALOG))

        assert (limits.max_servers, limits.max_tools, limits.max_skills) == (8, 60, None)

    def test_nothing_pickable_without_gateway(self) -> None:
        assert user_catalog_limits({}).model_dump() == {"max_servers": None, "max_tools": None, "max_skills": None}

    def test_lakebase_user_skills_set_the_skill_limit(self) -> None:
        limits = user_catalog_limits({"USER_SKILLS": UserSkills(store="lakebase", max_skills=7)})
        assert limits.model_dump() == {"max_servers": None, "max_tools": None, "max_skills": 7}


class TestToolsCandidate:
    def test_agent_service_keeps_its_identity_and_allow(self) -> None:
        config = _mcp_config(pinned=[{"fqn": "team.mcp.jira", "identity": "sp", "tools": {"allow": ["search"]}}])

        candidate = mcp_tools_candidate(config, "team.mcp.jira", _ws("sp"))

        assert (candidate.identity, candidate.origin, candidate.allow) == ("sp", "pinned", ("search",))

    def test_user_catalog_service_runs_as_user(self) -> None:
        candidate = mcp_tools_candidate(_mcp_config(user_catalog=CATALOG), "team.user_mcp.gmail", _ws("sp"))

        assert (candidate.identity, candidate.origin) == ("obo", "selected")

    @pytest.mark.parametrize("fqn", ["team.user_mcp.secret", "other.schema.svc", "not-an-fqn"])
    def test_refuses_what_the_agent_would_refuse(self, fqn: str) -> None:
        with pytest.raises(ResourceNotAllowedError):
            mcp_tools_candidate(_mcp_config(user_catalog=CATALOG), fqn, _ws("sp"))

    def test_refuses_without_gateway_mcp(self) -> None:
        with pytest.raises(ResourceNotAllowedError):
            mcp_tools_candidate({}, "team.mcp.jira", _ws("sp"))


class TestListMcpTools:
    @pytest.mark.asyncio
    async def test_obo_service_needs_user_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DATABRICKS_APP_NAME", "test-app")
        with pytest.raises(UserTokenRequiredError):
            await list_mcp_tools(Candidate("team.mcp.jira", "obo", "selected"), _ws("sp"), None, 5.0)

    @pytest.mark.asyncio
    async def test_connects_lists_and_cleans_up(self) -> None:
        server = _server(AsyncMock(return_value=[SimpleNamespace(name="search", description=None)]))
        user_ws = _ws("user")

        with patch.object(gateway_resources, "gateway_mcp_server", return_value=server) as build:
            response = await list_mcp_tools(Candidate("team.mcp.jira", "obo", "selected"), _ws("sp"), user_ws, 5.0)

        assert [(tool.name, tool.description) for tool in response.tools] == [("search", "")]
        assert build.call_args.args[2] is user_ws
        server.__aexit__.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cleans_up_when_listing_fails(self) -> None:
        server = _server(AsyncMock(side_effect=RuntimeError("boom")))

        with (
            patch.object(gateway_resources, "gateway_mcp_server", return_value=server),
            pytest.raises(RuntimeError),
        ):
            await list_mcp_tools(Candidate("team.mcp.jira", "sp", "pinned"), _ws("sp"), None, 5.0)

        server.__aexit__.assert_awaited_once()
