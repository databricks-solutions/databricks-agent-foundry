"""Unity Gateway MCP mounting (``spec.runtime.tools.mcp.gateway``)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp.types import Tool

from payloads.agent.contracts import GatewayMcpRuntime
from payloads.agent.runtime import gateway_mcp
from payloads.agent.runtime.brick_mcp_servers import (
    _BrickSpecLoad,
    _clear_brick_specs_cache,
    build_mcp_servers,
    build_mcp_servers_with_status,
)
from payloads.agent.runtime.gateway_mcp import (
    GatewayMcpServer,
    build_gateway_mcp_servers,
    build_gateway_mcp_servers_with_status,
    client_for_identity,
    enforce_tool_rules,
    enforce_tool_rules_with_status,
    mcp_service_url,
    resolution_attributes,
)
from payloads.agent.runtime.gateway_resolve import Candidate, Dropped, Origin, Resolution
from payloads.agent.runtime.gateway_selection import Selections, parse_selections
from payloads.agent.runtime.mcp_availability import McpBuild, UnavailableServer
from payloads.agent.runtime.mcp_tools_cache import SERVICE_PRINCIPAL_SCOPE, identity_scope
from payloads.agent.runtime.mcp_wrapper import FilteredMCPServerStreamableHttp

HOST = "https://example.cloud.databricks.com"
NO_PICKS = Selections()


@pytest.fixture(autouse=True)
def oauth(monkeypatch: pytest.MonkeyPatch) -> Iterator[MagicMock]:
    provider = MagicMock(name="oauth")
    monkeypatch.setenv("DATABRICKS_APP_NAME", "test-app")
    monkeypatch.setattr("payloads.agent.runtime.gateway_mcp.DatabricksOAuthClientProvider", provider)
    gateway_mcp._reported_drops.clear()
    yield provider
    gateway_mcp._reported_drops.clear()


def _sp_ws() -> MagicMock:
    ws = MagicMock(name="sp_ws")
    ws.config.host = HOST
    return ws


def _build(gateway: GatewayMcpRuntime, sp_ws: MagicMock, user_ws: MagicMock | None) -> list[GatewayMcpServer]:
    return build_gateway_mcp_servers(gateway, sp_ws, user_ws, selections=NO_PICKS, session_timeout_seconds=30.0)


def _gateway(*pinned: dict[str, object]) -> GatewayMcpRuntime:
    return GatewayMcpRuntime.model_validate({"pinned": list(pinned)})


class TestMcpServiceUrl:
    def test_builds_gateway_path(self) -> None:
        assert mcp_service_url(HOST + "/", "main.tools.github") == f"{HOST}/ai-gateway/mcp-services/main.tools.github"

    @pytest.mark.parametrize(
        "fqn",
        ["main.tools", "main.tools.github/../x", "main.tools.git hub", "main.tools.github?x=1", "a.b.c.d", ""],
    )
    def test_rejects_non_fqn(self, fqn: str) -> None:
        with pytest.raises(ValueError, match="not a Unity Catalog MCP service name"):
            mcp_service_url(HOST, fqn)


class TestClientForIdentity:
    def test_sp_ignores_user_token(self) -> None:
        sp_ws, user_ws = MagicMock(), MagicMock()
        assert client_for_identity("sp", sp_ws, user_ws) is sp_ws

    def test_obo_uses_user_client(self) -> None:
        sp_ws, user_ws = MagicMock(), MagicMock()
        assert client_for_identity("obo", sp_ws, user_ws) is user_ws

    def test_obo_without_token_is_dropped(self) -> None:
        assert client_for_identity("obo", MagicMock(), None) is None

    def test_obo_without_token_ignores_the_removed_fallback_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENT_FOUNDRY_ALLOW_SP_FALLBACK", "1")
        assert client_for_identity("obo", MagicMock(), None) is None

    def test_obo_without_token_uses_the_profile_client_in_local_dev(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DATABRICKS_APP_NAME")
        local_ws = MagicMock()
        assert client_for_identity("obo", local_ws, None) is local_ws


class TestBuildGatewayMcpServers:
    def test_sorted_by_fqn_with_gateway_urls(self) -> None:
        gateway = _gateway({"fqn": "main.tools.zeta", "identity": "sp"}, {"fqn": "main.tools.alpha", "identity": "sp"})

        servers = _build(gateway, _sp_ws(), None)

        assert [s.name for s in servers] == ["gateway-main.tools.alpha", "gateway-main.tools.zeta"]
        assert servers[0].params["url"] == f"{HOST}/ai-gateway/mcp-services/main.tools.alpha"

    def test_auth_bound_to_pinned_identity(self, oauth: MagicMock) -> None:
        sp_ws, user_ws = _sp_ws(), MagicMock(name="user_ws")
        gateway = _gateway({"fqn": "main.tools.a", "identity": "sp"}, {"fqn": "main.tools.b", "identity": "obo"})

        _build(gateway, sp_ws, user_ws)

        assert [c.args[0] for c in oauth.call_args_list] == [sp_ws, user_ws]

    def test_obo_without_token_dropped_and_logged_once(self, caplog: pytest.LogCaptureFixture) -> None:
        gateway = _gateway({"fqn": "main.tools.a", "identity": "sp"}, {"fqn": "main.tools.b", "identity": "obo"})

        with caplog.at_level("WARNING", logger=gateway_mcp.__name__):
            first = _build(gateway, _sp_ws(), None)
            _build(gateway, _sp_ws(), None)

        assert [s.name for s in first] == ["gateway-main.tools.a"]
        assert sum("main.tools.b" in r.getMessage() for r in caplog.records) == 1

    def test_tool_lists_keyed_by_the_identity_they_run_as(self) -> None:
        gateway = _gateway({"fqn": "main.tools.a", "identity": "sp"}, {"fqn": "main.tools.b", "identity": "obo"})

        sp, obo = build_gateway_mcp_servers(
            gateway,
            _sp_ws(),
            MagicMock(name="user_ws"),
            selections=NO_PICKS,
            session_timeout_seconds=30.0,
            user_cache_scope="user-digest",
        )

        assert sp._tools_cache_key() == (mcp_service_url(HOST, "main.tools.a"), SERVICE_PRINCIPAL_SCOPE)
        assert obo._tools_cache_key() == (mcp_service_url(HOST, "main.tools.b"), "user-digest")

    def test_obo_tool_list_uncached_without_a_user_scope(self) -> None:
        (server,) = _build(_gateway({"fqn": "main.tools.b", "identity": "obo"}), _sp_ws(), MagicMock(name="user_ws"))

        assert server._tools_cache_key() is None

    def test_obo_without_token_reported_unavailable(self) -> None:
        gateway = _gateway({"fqn": "main.tools.b", "identity": "obo"})

        build = build_gateway_mcp_servers_with_status(
            gateway, _sp_ws(), None, selections=NO_PICKS, session_timeout_seconds=30.0
        )

        assert build.servers == []
        assert build.unavailable == (UnavailableServer("main.tools.b", "no_user_token"),)

    def test_empty_allow_leaves_service_in_charge(self) -> None:
        gateway = _gateway({"fqn": "main.tools.a", "identity": "sp"})

        (server,) = _build(gateway, _sp_ws(), None)

        assert server.allow_entries is None

    def test_allow_narrows_tools(self) -> None:
        gateway = _gateway({"fqn": "main.tools.a", "identity": "sp", "tools": {"allow": ["search_*", "get_issue"]}})

        (server,) = _build(gateway, _sp_ws(), None)

        assert server.allow_entries is not None
        assert [e.match for e in server.allow_entries] == ["search_*", "get_issue"]


class TestBuildMcpServersDispatch:
    @patch("payloads.agent.runtime.brick_mcp_servers.get_user_workspace_client", return_value=None)
    @patch("payloads.agent.runtime.brick_mcp_servers.resolve_instance_dir", return_value=None)
    def test_gateway_only_instance_mounts_gateway(self, _resolve: MagicMock, _user: MagicMock) -> None:
        config = {"GATEWAY_MCP": _gateway({"fqn": "main.tools.a", "identity": "sp"})}

        servers = build_mcp_servers(config, _sp_ws())

        assert [s.name for s in servers] == ["gateway-main.tools.a"]

    @patch("payloads.agent.runtime.brick_mcp_servers.resolve_instance_dir", return_value=None)
    @patch("payloads.agent.runtime.brick_mcp_servers.get_user_workspace_client")
    def test_selections_reach_gateway(self, mock_user: MagicMock, _resolve: MagicMock) -> None:
        mock_user.return_value = MagicMock(name="user_ws")
        gateway = GatewayMcpRuntime.model_validate({"user_catalog": {"enabled": True, "locations": ["team.mine"]}})
        picks = parse_selections({"selected_resources": [{"kind": "mcp_service", "fqn": "team.mine.jira"}]})

        servers = build_mcp_servers({"GATEWAY_MCP": gateway}, _sp_ws(), selections=picks)

        assert [s.name for s in servers] == ["gateway-team.mine.jira"]

    @patch("payloads.agent.runtime.brick_mcp_servers.resolve_instance_dir", return_value=None)
    @patch("payloads.agent.runtime.brick_mcp_servers.get_obo_token", return_value="user-token")
    @patch("payloads.agent.runtime.brick_mcp_servers.get_user_workspace_client")
    def test_obo_tool_lists_keyed_by_the_caller(
        self, mock_user: MagicMock, _token: MagicMock, _resolve: MagicMock
    ) -> None:
        mock_user.return_value = MagicMock(name="user_ws")
        gateway = _gateway({"fqn": "main.tools.b", "identity": "obo"})

        (server,) = build_mcp_servers({"GATEWAY_MCP": gateway}, _sp_ws())

        assert server._tools_cache_key() == (mcp_service_url(HOST, "main.tools.b"), identity_scope("user-token"))

    @patch("payloads.agent.runtime.brick_mcp_servers.get_user_workspace_client", return_value=None)
    @patch(
        "payloads.agent.runtime.brick_mcp_servers._workspace_mcp_build",
        return_value=McpBuild(
            servers=[cast(Any, MagicMock(name="brick-server"))],
            unavailable=(UnavailableServer("jira", "invalid_state"),),
        ),
    )
    def test_mixed_sources_put_workspace_servers_first(self, mock_workspace: MagicMock, _user: MagicMock) -> None:
        gateway = _gateway({"fqn": "main.tools.a", "identity": "sp"}, {"fqn": "main.tools.b", "identity": "obo"})

        build = build_mcp_servers_with_status({"GATEWAY_MCP": gateway}, _sp_ws())

        brick_server = mock_workspace.return_value.servers[0]
        assert build.servers[0] is brick_server
        assert [s.name for s in build.servers[1:]] == ["gateway-main.tools.a"]
        assert build.unavailable == (
            UnavailableServer("jira", "invalid_state"),
            UnavailableServer("main.tools.b", "no_user_token"),
        )

    @patch("payloads.agent.runtime.gateway_mcp.build_gateway_mcp_servers_with_status")
    @patch("payloads.agent.runtime.brick_mcp_servers._mcp_servers_from_specs")
    @patch(
        "payloads.agent.runtime.brick_mcp_servers._load_brick_spec_status",
        return_value=_BrickSpecLoad(specs=(cast(Any, MagicMock()),)),
    )
    @patch("payloads.agent.runtime.request_context.get_user_workspace_client", return_value=None)
    @patch("payloads.agent.runtime.brick_mcp_servers.resolve_instance_dir")
    def test_without_gateway_block_mounts_workspace_only(
        self,
        mock_resolve: MagicMock,
        _user: MagicMock,
        _load: MagicMock,
        mock_construct: MagicMock,
        mock_gateway: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("DEPLOY_TARGET", "dev")
        monkeypatch.delenv("DATABRICKS_APP_NAME")
        mock_resolve.return_value = tmp_path
        _clear_brick_specs_cache()

        servers = build_mcp_servers({"GATEWAY_MCP": None}, _sp_ws())

        mock_gateway.assert_not_called()
        assert servers is mock_construct.return_value
        _clear_brick_specs_cache()


def _tools(*names: str) -> list[Tool]:
    return [Tool(name=name, inputSchema={"type": "object"}) for name in names]


def _server(
    monkeypatch: pytest.MonkeyPatch,
    fqn: str,
    origin: Origin,
    tool_names: tuple[str, ...],
) -> GatewayMcpServer:
    candidate = Candidate(fqn, "sp", origin)
    server = GatewayMcpServer(params={"url": f"{HOST}/{fqn}"}, candidate=candidate, name=f"gateway-{fqn}")
    monkeypatch.setattr(server, "list_tools", AsyncMock(return_value=_tools(*tool_names)))
    return server


def _brick_server(monkeypatch: pytest.MonkeyPatch, tool_names: tuple[str, ...]) -> MagicMock:
    server = MagicMock(name="brick-server")
    monkeypatch.setattr(server, "list_tools", AsyncMock(return_value=_tools(*tool_names)))
    return server


class TestGatewayMcpServer:
    @pytest.mark.asyncio
    async def test_excluded_tools_hidden_and_sorted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            FilteredMCPServerStreamableHttp, "list_tools", AsyncMock(return_value=_tools("zeta", "delete_x", "alpha"))
        )
        candidate = Candidate("main.tools.a", "obo", "selected", exclude_tools=("delete_*",))
        server = GatewayMcpServer(params={"url": HOST}, candidate=candidate, name="gateway-main.tools.a")

        assert [t.name for t in await server.list_tools()] == ["alpha", "zeta"]

    @pytest.mark.asyncio
    async def test_excluded_tool_call_refused(self) -> None:
        candidate = Candidate("main.tools.a", "obo", "selected", exclude_tools=("delete_*",))
        server = GatewayMcpServer(params={"url": HOST}, candidate=candidate, name="gateway-main.tools.a")

        with pytest.raises(PermissionError, match="excluded"):
            await server.call_tool("delete_repo", {})


class TestEnforceToolRules:
    @pytest.mark.asyncio
    async def test_collision_drops_later_server_and_keeps_pinned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pinned = _server(monkeypatch, "main.tools.a", "pinned", ("search",))
        discovered = _server(monkeypatch, "main.tools.b", "discovered", ("search", "get"))
        other = _brick_server(monkeypatch, ("list_rows",))

        kept = await enforce_tool_rules(_gateway(), [other, pinned, discovered])

        assert kept == [other, pinned]

    @pytest.mark.asyncio
    async def test_workspace_tool_names_win_over_gateway(self, monkeypatch: pytest.MonkeyPatch) -> None:
        brick = _brick_server(monkeypatch, ("search",))
        pinned = _server(monkeypatch, "main.tools.a", "pinned", ("search",))
        selected = _server(monkeypatch, "main.tools.b", "selected", ("get",))

        build = await enforce_tool_rules_with_status(_gateway(), [brick, pinned, selected])

        assert build.servers == [brick, selected]
        assert build.unavailable == (UnavailableServer("main.tools.a", "tool_name_collision"),)

    @pytest.mark.asyncio
    async def test_over_tool_limit_drops_server(self, monkeypatch: pytest.MonkeyPatch) -> None:
        gateway = GatewayMcpRuntime.model_validate({"user_catalog": {"limits": {"max_tools": 3}}})
        first = _server(monkeypatch, "main.tools.a", "selected", ("a1", "a2"))
        second = _server(monkeypatch, "main.tools.b", "selected", ("b1", "b2"))

        kept = await enforce_tool_rules(gateway, [first, second])

        assert kept == [first]

    @pytest.mark.asyncio
    async def test_pinned_tools_not_limited(self, monkeypatch: pytest.MonkeyPatch) -> None:
        gateway = GatewayMcpRuntime.model_validate({"discovery": {"limits": {"max_tools": 1}}})
        pinned = _server(monkeypatch, "main.tools.a", "pinned", ("a1", "a2", "a3"))

        assert await enforce_tool_rules(gateway, [pinned]) == [pinned]

    @pytest.mark.asyncio
    async def test_collision_reports_chosen_services_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pinned = _server(monkeypatch, "main.tools.a", "pinned", ("search",))
        selected = _server(monkeypatch, "main.tools.b", "selected", ("search",))
        discovered = _server(monkeypatch, "main.tools.c", "discovered", ("search",))

        build = await enforce_tool_rules_with_status(_gateway(), [pinned, selected, discovered])

        assert build.servers == [pinned]
        assert build.unavailable == (UnavailableServer("main.tools.b", "tool_name_collision"),)

    @pytest.mark.asyncio
    async def test_over_tool_limit_not_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        gateway = GatewayMcpRuntime.model_validate({"user_catalog": {"limits": {"max_tools": 1}}})
        selected = _server(monkeypatch, "main.tools.a", "selected", ("a1", "a2"))

        build = await enforce_tool_rules_with_status(gateway, [selected])

        assert build.servers == []
        assert build.unavailable == ()


class TestResolutionAttributes:
    def test_names_identities_and_reasons_only(self) -> None:
        resolution = Resolution(
            mounted=(Candidate("main.tools.a", "sp", "pinned"),),
            dropped=(Dropped("main.other.x", "selected", "outside_locations"),),
        )
        attributes = resolution_attributes(_gateway(), resolution, Selections(rejected="invalid"))

        assert attributes["gateway.mcp.mounted"] == ["main.tools.a|sp|pinned"]
        assert attributes["gateway.mcp.dropped"] == ["main.other.x|selected|outside_locations"]
        assert attributes["gateway.selections.rejected"] == "invalid"
        assert len(attributes["gateway.config_hash"]) == 16
        assert not any("token" in str(value).lower() for value in attributes.values())

    def test_missing_token_recorded_as_dropped(self) -> None:
        gateway = _gateway({"fqn": "main.tools.b", "identity": "obo"})

        with patch("mlflow.start_span") as start_span:
            _build(gateway, _sp_ws(), None)

        span = start_span.return_value.__enter__.return_value
        attributes = span.set_attributes.call_args.args[0]
        assert attributes["gateway.mcp.dropped"] == ["main.tools.b|pinned|no_user_token"]
