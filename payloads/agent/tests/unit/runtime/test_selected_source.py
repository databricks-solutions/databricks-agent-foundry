"""User selections are untrusted: shape checks, static policy, and no listing calls."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from payloads.agent.contracts import GatewayMcpRuntime
from payloads.agent.runtime.gateway_resolve import resolve_request
from payloads.agent.runtime.gateway_selection import MAX_SELECTIONS, parse_selections

CATALOG = {"enabled": True, "locations": ["team.user_mcp"], "exclude": ["team.user_mcp.gmail"]}


def _gateway(**blocks: Any) -> GatewayMcpRuntime:
    return GatewayMcpRuntime.model_validate({"user_catalog": CATALOG, **blocks})


def _picks(*fqns: str) -> dict[str, Any]:
    return {"selected_resources": [{"kind": "mcp_service", "fqn": fqn} for fqn in fqns]}


def _resolve(gateway: GatewayMcpRuntime, custom_inputs: dict[str, Any] | None) -> tuple[MagicMock, Any]:
    ws = MagicMock(name="sp_ws")
    return ws, resolve_request(gateway, ws, parse_selections(custom_inputs))


class TestPolicy:
    @pytest.mark.parametrize(
        ("fqn", "reason"),
        [
            ("team.user_mcp.x/../../etc", "invalid_fqn"),
            ("team.user_mcp", "invalid_fqn"),
            ("other.schema.jira", "outside_locations"),
            ("team.user_mcp.gmail", "excluded"),
        ],
    )
    def test_forged_and_disallowed_dropped_and_recorded(self, fqn: str, reason: str) -> None:
        _, result = _resolve(_gateway(), _picks(fqn))

        assert result.mounted == ()
        assert [(d.fqn, d.origin, d.reason) for d in result.dropped] == [(fqn, "selected", reason)]

    def test_catalog_disabled_drops_everything(self) -> None:
        gateway = GatewayMcpRuntime.model_validate({"user_catalog": {"enabled": False}})
        _, result = _resolve(gateway, _picks("team.user_mcp.jira"))

        assert result.mounted == ()
        assert result.dropped[0].reason == "disabled"

    def test_valid_pick_mounted_as_user(self) -> None:
        _, result = _resolve(_gateway(), _picks("team.user_mcp.jira"))
        assert [(c.fqn, c.identity, c.origin) for c in result.mounted] == [("team.user_mcp.jira", "obo", "selected")]

    def test_duplicate_of_baseline_ignored(self) -> None:
        gateway = _gateway(pinned=[{"fqn": "team.user_mcp.jira", "identity": "sp"}])
        _, result = _resolve(gateway, _picks("team.user_mcp.jira"))

        assert [(c.fqn, c.origin) for c in result.mounted] == [("team.user_mcp.jira", "pinned")]
        assert result.dropped == ()

    def test_over_limit_rejected(self) -> None:
        gateway = GatewayMcpRuntime.model_validate({"user_catalog": {**CATALOG, "limits": {"max_servers": 1}}})
        _, result = _resolve(gateway, _picks("team.user_mcp.a", "team.user_mcp.b"))

        assert result.mounted == ()
        assert {d.reason for d in result.dropped} == {"over_limit"}

    def test_skill_picks_ignored_for_mcp(self) -> None:
        _, result = _resolve(_gateway(), {"selected_resources": [{"kind": "skill", "fqn": "team.user_mcp.s"}]})
        assert result.mounted == ()
        assert result.dropped == ()

    def test_no_listing_call(self) -> None:
        ws, _ = _resolve(_gateway(), _picks("team.user_mcp.jira", "other.schema.x"))
        ws.ai_gateway.list_mcp_services.assert_not_called()
        ws.ai_gateway.list_skills.assert_not_called()


class TestParse:
    @pytest.mark.parametrize(
        "raw",
        [
            "team.user_mcp.jira",
            [{"kind": "mcp_service"}],
            [{"kind": "agent", "fqn": "a.b.c"}],
            [{"kind": "mcp_service", "fqn": "a.b.c", "identity": "sp"}],
            [{"kind": "mcp_service", "fqn": "x" * 300}],
            [{"kind": "mcp_service", "fqn": f"a.b.c{i}"} for i in range(MAX_SELECTIONS + 1)],
        ],
    )
    def test_malformed_or_oversized_rejected_whole(self, raw: Any) -> None:
        selections = parse_selections({"selected_resources": raw})
        assert selections.resources == ()
        assert selections.rejected is not None

    @pytest.mark.parametrize("custom_inputs", [None, {}, {"include_reasoning": True}])
    def test_missing_means_baseline_only(self, custom_inputs: dict[str, Any] | None) -> None:
        selections = parse_selections(custom_inputs)
        assert selections.resources == ()
        assert selections.rejected is None
