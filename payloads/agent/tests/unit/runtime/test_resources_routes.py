"""HTTP surface of the sidebar resources API: auth header, status codes, gating."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from payloads.agent.contracts import GatewayMcpRuntime
from payloads.agent.runtime import resources_routes
from payloads.agent.runtime.gateway_resources import ResourcesResponse, ToolsResponse, UserCatalogLimits
from payloads.agent.runtime.resources_routes import RESOURCES_PATH, TOOLS_PATH, register_resources_routes

CATALOG = {"enabled": True, "locations": ["team.user_mcp"]}


@pytest.fixture
def config() -> dict[str, Any]:
    return {"GATEWAY_MCP": GatewayMcpRuntime.model_validate({"user_catalog": CATALOG})}


@pytest.fixture
def client(config: dict[str, Any]) -> Iterator[TestClient]:
    app = FastAPI()
    register_resources_routes(app)
    sp_ws = MagicMock(name="sp_ws")
    with (
        patch.object(resources_routes, "get_workspace_client", return_value=sp_ws),
        patch.object(resources_routes, "load_agent_config_and_vars", return_value=(config, {})),
        patch.object(resources_routes, "user_workspace_client", side_effect=lambda token: f"user:{token}"),
    ):
        yield TestClient(app)


class TestResources:
    def test_passes_the_user_token_through(self, client: TestClient) -> None:
        empty = ResourcesResponse(agent=[], user=[], limits=UserCatalogLimits())
        with patch.object(resources_routes, "build_resources", return_value=empty) as build:
            response = client.get(RESOURCES_PATH, headers={"x-forwarded-access-token": "tok"})

        assert response.status_code == 200
        assert response.json()["agent"] == []
        assert build.call_args.args[2] == "user:tok"

    def test_without_token_builds_for_no_user(self, client: TestClient) -> None:
        response = client.get(RESOURCES_PATH)

        assert response.status_code == 200
        assert response.json()["user_reason"] == "no_user_token"

    def test_workspace_mode_lists_server_names(self, client: TestClient, config: dict[str, Any]) -> None:
        config.clear()
        with patch.object(resources_routes, "workspace_mcp_server_names", return_value=["github"]):
            body = client.get(RESOURCES_PATH).json()

        assert [entry["fqn"] for entry in body["agent"]] == ["github"]

    def test_mixed_mode_lists_server_names_next_to_user_catalog(self, client: TestClient) -> None:
        with patch.object(resources_routes, "workspace_mcp_server_names", return_value=["github"]):
            body = client.get(RESOURCES_PATH).json()

        assert [(entry["fqn"], entry["source"]) for entry in body["agent"]] == [("github", "workspace")]
        assert body["limits"]["max_servers"] == 8


class TestTools:
    def test_unknown_service_is_404(self, client: TestClient) -> None:
        response = client.get(TOOLS_PATH, params={"fqn": "other.schema.svc"})

        assert response.status_code == 404

    def test_missing_fqn_is_422(self, client: TestClient) -> None:
        assert client.get(TOOLS_PATH).status_code == 422

    def test_user_service_without_token_is_401(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DATABRICKS_APP_NAME", "test-app")
        response = client.get(TOOLS_PATH, params={"fqn": "team.user_mcp.gmail"})

        assert response.status_code == 401

    def test_connection_failure_is_502_without_details(self, client: TestClient) -> None:
        failing = AsyncMock(side_effect=RuntimeError("secret upstream detail"))
        with patch.object(resources_routes, "list_mcp_tools", failing):
            response = client.get(
                TOOLS_PATH, params={"fqn": "team.user_mcp.gmail"}, headers={"x-forwarded-access-token": "tok"}
            )

        assert response.status_code == 502
        assert "secret" not in response.text

    def test_lists_tools_as_user(self, client: TestClient) -> None:
        tools = AsyncMock(return_value=ToolsResponse(fqn="team.user_mcp.gmail", tools=[]))
        with patch.object(resources_routes, "list_mcp_tools", tools):
            response = client.get(
                TOOLS_PATH, params={"fqn": "team.user_mcp.gmail"}, headers={"x-forwarded-access-token": "tok"}
            )

        assert response.status_code == 200
        assert tools.call_args.args[0].identity == "obo"
        assert tools.call_args.args[2] == "user:tok"


class TestStartServerGate:
    @pytest.mark.parametrize(("flag", "mounted"), [("true", True), ("false", False)])
    def test_routes_follow_the_env_toggle(self, monkeypatch: pytest.MonkeyPatch, flag: str, mounted: bool) -> None:
        from payloads.agent.runtime import start_server

        monkeypatch.setenv("ENABLE_RESOURCES_SIDEBAR", flag)
        app = FastAPI()
        server = MagicMock(app=app, chat_proxy_timeout=1.0)
        with (
            patch("mlflow.genai.agent_server.AgentServer", return_value=server),
            patch("uvicorn.run"),
            # Last: ``patch`` resolves its targets through importlib.
            patch("importlib.import_module"),
        ):
            start_server.main(["--agent-module", "unused"])

        paths = {getattr(route, "path", None) for route in app.routes}
        assert (RESOURCES_PATH in paths) is mounted
        assert (TOOLS_PATH in paths) is mounted
