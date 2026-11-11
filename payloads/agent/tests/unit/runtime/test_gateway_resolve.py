"""Resolution of gateway MCP candidates: policy, de-duplication, limits, ordering."""

from __future__ import annotations

import random
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

import pytest
from databricks.sdk.errors import PermissionDenied
from databricks.sdk.service.catalog import McpService, Skill

from payloads.agent.contracts import GatewayMcpRuntime, GatewaySkillsRuntime
from payloads.agent.runtime.gateway_resolve import (
    LISTING_TTL_SECONDS,
    Candidate,
    GatewayLimitError,
    PinnedSource,
    SpDiscoverySource,
    clear_listing_cache,
    resolve,
    resolve_request,
)
from payloads.agent.runtime.gateway_selection import SelectedResource, Selections


@pytest.fixture(autouse=True)
def _fresh_cache() -> Iterator[None]:
    clear_listing_cache()
    yield
    clear_listing_cache()


def _gateway(**blocks: Any) -> GatewayMcpRuntime:
    return GatewayMcpRuntime.model_validate(blocks)


def _sp_ws(*fqns: str) -> MagicMock:
    ws = MagicMock(name="sp_ws")
    ws.config.client_id = "app-sp"
    ws.ai_gateway.list_mcp_services.return_value = [McpService(name=f"mcp-services/{fqn}") for fqn in fqns]
    return ws


DISCOVERY = {"enabled": True, "locations": ["team.mcp"]}
CATALOG = {"enabled": True, "locations": ["team.user_mcp"]}


def _found(*fqns: str) -> list[Candidate]:
    return [Candidate(fqn, "sp", "discovered") for fqn in fqns]


def _picked(*fqns: str) -> list[Candidate]:
    return [Candidate(fqn, "obo", "selected") for fqn in fqns]


class TestResolve:
    def test_pinned_wins_on_duplicate(self) -> None:
        gateway = _gateway(pinned=[{"fqn": "team.mcp.jira", "identity": "obo"}], discovery=DISCOVERY)

        result = resolve(gateway, pinned=PinnedSource(gateway).candidates(), discovered=_found("team.mcp.jira"))

        assert [(c.fqn, c.identity, c.origin) for c in result.mounted] == [("team.mcp.jira", "obo", "pinned")]
        assert result.dropped == ()

    def test_include_and_exclude_globs(self) -> None:
        gateway = _gateway(discovery={**DISCOVERY, "include": ["team.mcp.j*", "team.mcp.k*"], "exclude": ["*.kafka"]})

        result = resolve(gateway, pinned=[], discovered=_found("team.mcp.jira", "team.mcp.kafka", "team.mcp.slack"))

        assert [c.fqn for c in result.mounted] == ["team.mcp.jira"]
        assert {(d.fqn, d.reason) for d in result.dropped} == {
            ("team.mcp.kafka", "excluded"),
            ("team.mcp.slack", "not_included"),
        }

    def test_discovery_over_limit_raises(self) -> None:
        gateway = _gateway(discovery={**DISCOVERY, "limits": {"max_servers": 1}})

        with pytest.raises(GatewayLimitError, match="max_servers=1"):
            resolve(gateway, pinned=[], discovered=_found("team.mcp.a", "team.mcp.b"))

    def test_fixed_order(self) -> None:
        gateway = _gateway(
            pinned=[{"fqn": "z.pinned.b", "identity": "sp"}, {"fqn": "z.pinned.a", "identity": "sp"}],
            discovery=DISCOVERY,
            user_catalog=CATALOG,
        )

        result = resolve(
            gateway,
            pinned=PinnedSource(gateway).candidates(),
            discovered=_found("team.mcp.y", "team.mcp.x"),
            selected=_picked("team.user_mcp.d", "team.user_mcp.c"),
        )

        assert [c.fqn for c in result.mounted] == [
            "z.pinned.a",
            "z.pinned.b",
            "team.mcp.x",
            "team.mcp.y",
            "team.user_mcp.c",
            "team.user_mcp.d",
        ]

    def test_shuffled_input_same_output(self) -> None:
        gateway = _gateway(discovery=DISCOVERY, user_catalog=CATALOG)
        found = _found("team.mcp.a", "team.mcp.b", "team.mcp.c")
        picked = _picked("team.user_mcp.a", "team.user_mcp.b")
        expected = resolve(gateway, pinned=[], discovered=found, selected=picked)

        rng = random.Random(7)
        for _ in range(5):
            rng.shuffle(found)
            rng.shuffle(picked)
            assert resolve(gateway, pinned=[], discovered=found, selected=picked) == expected


class TestSpDiscovery:
    def test_lists_each_location_as_sp(self) -> None:
        gateway = _gateway(discovery={"enabled": True, "locations": ["team.mcp", "ops.mcp"]})
        ws = _sp_ws("team.mcp.jira")

        SpDiscoverySource(gateway, ws).candidates()

        parents = [c.kwargs["parent"] for c in ws.ai_gateway.list_mcp_services.call_args_list]
        assert parents == ["schemas/team.mcp", "schemas/ops.mcp"]

    def test_disabled_lists_nothing(self) -> None:
        ws = _sp_ws()
        assert SpDiscoverySource(_gateway(), ws).candidates() == []
        ws.ai_gateway.list_mcp_services.assert_not_called()

    def test_cached_for_ttl(self) -> None:
        gateway = _gateway(discovery=DISCOVERY)
        ws = _sp_ws("team.mcp.jira")
        now = [1000.0]

        SpDiscoverySource(gateway, ws, clock=lambda: now[0]).candidates()
        now[0] += LISTING_TTL_SECONDS - 1
        SpDiscoverySource(gateway, ws, clock=lambda: now[0]).candidates()
        assert ws.ai_gateway.list_mcp_services.call_count == 1

        now[0] += 2
        SpDiscoverySource(gateway, ws, clock=lambda: now[0]).candidates()
        assert ws.ai_gateway.list_mcp_services.call_count == 2

    def test_failure_keeps_pinned(self) -> None:
        gateway = _gateway(pinned=[{"fqn": "team.pinned.jira", "identity": "sp"}], discovery=DISCOVERY)
        ws = _sp_ws()
        ws.ai_gateway.list_mcp_services.side_effect = PermissionDenied("no")

        result = resolve_request(gateway, ws, Selections())

        assert [c.fqn for c in result.mounted] == ["team.pinned.jira"]

    def test_limit_error_keeps_pinned_and_picks(self) -> None:
        gateway = _gateway(
            pinned=[{"fqn": "team.pinned.jira", "identity": "sp"}],
            discovery={**DISCOVERY, "limits": {"max_servers": 1}},
            user_catalog=CATALOG,
        )
        ws = _sp_ws("team.mcp.a", "team.mcp.b")
        picks = Selections(resources=(SelectedResource(kind="mcp_service", fqn="team.user_mcp.x"),))

        result = resolve_request(gateway, ws, picks)

        assert [c.fqn for c in result.mounted] == ["team.pinned.jira", "team.user_mcp.x"]


class TestSkillKind:
    """A ``GatewaySkillsRuntime`` resolves skills through the same step."""

    def test_discovery_lists_skills_not_mcp_services(self) -> None:
        gateway = GatewaySkillsRuntime.model_validate({"discovery": {"enabled": True, "locations": ["team.skills"]}})
        ws = _sp_ws()
        ws.ai_gateway.list_skills.return_value = [Skill(name="skills/team.skills.triage")]

        candidates = SpDiscoverySource(gateway, ws).candidates()

        assert [c.fqn for c in candidates] == ["team.skills.triage"]
        ws.ai_gateway.list_mcp_services.assert_not_called()

    def test_only_skill_picks_selected(self) -> None:
        gateway = GatewaySkillsRuntime.model_validate({"user_catalog": {"enabled": True, "locations": ["team.u"]}})
        picks = Selections(
            resources=(
                SelectedResource(kind="skill", fqn="team.u.notes"),
                SelectedResource(kind="mcp_service", fqn="team.u.jira"),
            )
        )

        result = resolve_request(gateway, _sp_ws(), picks)

        assert [c.fqn for c in result.mounted] == ["team.u.notes"]

    def test_discovery_over_max_skills_raises(self) -> None:
        gateway = GatewaySkillsRuntime.model_validate(
            {"discovery": {"enabled": True, "locations": ["team.mcp"], "limits": {"max_skills": 1}}}
        )

        with pytest.raises(GatewayLimitError, match="max_skills=1"):
            resolve(gateway, pinned=[], discovered=_found("team.mcp.a", "team.mcp.b"))
