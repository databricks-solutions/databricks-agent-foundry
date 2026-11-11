"""Tests for the MCP provision step + its ordering guarantee (issue #338).

Two layers:

* :class:`McpStep` in isolation -- self-gating and the ``mcp__<sub>``
  variable publishing it inherits from the wrapped ``MCPBrick``;
* the **central correctness pin**: an end-to-end ``AppBrick.deploy``
  asserting that the ``mcp__<sub>`` variable an MCP server publishes is
  present in the variable bag handed to the App deploy -- i.e. the
  provision manifest runs (and MCP publishes) *before* ``app.yaml`` is
  rendered.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from agent_foundry._instance_layout import payload_root, resolve_payload_subdir
from agent_foundry.bricks import DeployContext, DestroyContext
from bricks.app.brick import AppBrick
from payloads.agent.deploy.mcp import McpStep
from payloads.agent.tests.unit.deploy.mcp.conftest import FakeWorkspaceClient, write_server_yml

_HOST = "https://test.databricks.net"


def _variables() -> dict[str, str]:
    return {"host": _HOST, "app_name": "demo", "chat_history_enabled": "false"}


@pytest.fixture
def agent_mcp_instance(tmp_path: Path) -> Path:
    """An agent-payload instance with one MCP server.

    The payload's single config contract is
    ``payloads/agent/payload.yml``; chat history is disabled so the
    end-to-end App deploy test exercises the MCP -> render ordering
    without the Lakebase provision step touching a real workspace.
    MCP servers now live under the payload root
    (``payloads/agent/mcp_servers/``).
    """
    inst = tmp_path / "instances" / "demo"
    agent_payload = payload_root(inst)
    agent_payload.mkdir(parents=True)
    (agent_payload / "payload.yml").write_text(
        "payload:\n"
        "  adapter: agent\n"
        "  spec:\n"
        "    runtime:\n"
        '      system_prompt: "test"\n'
        "    ui:\n"
        "      chat_history:\n"
        "        enabled: false\n",
        encoding="utf-8",
    )
    write_server_yml(
        resolve_payload_subdir(inst, "mcp_servers"),
        "sql",
        "type: managed_sql\ntools:\n  allow: ['exec_sql']\n",
    )
    return inst


class TestMcpStep:
    def test_noop_without_mcp_servers(self, tmp_path: Path) -> None:
        ctx = DeployContext(instance_dir=tmp_path, target="dev")
        assert McpStep().provision(ctx) == 0
        assert ctx.variables == {}

    def test_publishes_mcp_var(self, agent_mcp_instance: Path) -> None:
        fake_ws = FakeWorkspaceClient()
        ctx = DeployContext(
            instance_dir=agent_mcp_instance,
            target="dev",
            variables=_variables(),
            ws=fake_ws,
        )
        with patch("payloads.agent.deploy.mcp.deploy._get_client", return_value=fake_ws):
            rc = McpStep().provision(ctx)

        assert rc == 0
        assert ctx.variables["mcp__sql"] == f"{_HOST}/api/2.0/mcp/sql"

    def test_plan_destroy_and_destroy_delegate(self, agent_mcp_instance: Path) -> None:
        fake_ws = FakeWorkspaceClient()
        deploy_ctx = DeployContext(
            instance_dir=agent_mcp_instance,
            target="dev",
            variables=_variables(),
            ws=fake_ws,
        )
        with patch("payloads.agent.deploy.mcp.deploy._get_client", return_value=fake_ws):
            McpStep().provision(deploy_ctx)

        destroy_ctx = DestroyContext(
            instance_dir=agent_mcp_instance,
            target="dev",
            variables=_variables(),
            ws=fake_ws,
            force=True,
        )
        plan = McpStep().plan_destroy(destroy_ctx)
        assert [item.sub_instance for item in plan] == ["sql"]
        assert all(item.destructive is False for item in plan)

        results = McpStep().destroy(destroy_ctx)
        assert [r.name for r in results] == ["sql"]
        assert all(r.status == "ok" for r in results)


class TestMcpVarReachesAppRender:
    """Pins the provision-before-render ordering (issue #338): the
    ``mcp__<sub>`` variable MCP publishes must be in the variable bag the
    App deploy renders ``app.yaml`` from."""

    def test_app_deploy_sees_mcp_var(self, agent_mcp_instance: Path) -> None:
        fake_ws = FakeWorkspaceClient()
        ctx = DeployContext(
            instance_dir=agent_mcp_instance,
            target="dev",
            variables=_variables(),
            ws=fake_ws,
        )
        captured: dict[str, str] = {}

        def _fake_deploy(
            app_resource: str,
            target: str,
            variables: dict[str, str],
            *,
            instance_dir: Path,
            dry_run: bool,
            **_k: object,
        ) -> int:
            captured.update(variables)
            return 0

        with (
            patch("payloads.agent.deploy.mcp.deploy._get_client", return_value=fake_ws),
            patch("bricks.app.scripts.deploy_agent_app.deploy", side_effect=_fake_deploy),
        ):
            results = AppBrick().deploy(ctx)

        assert all(r.status == "ok" for r in results)
        # The App deploy (which renders app.yaml) received the freshly
        # published MCP URL variable -> provision ran before render.
        assert captured["mcp__sql"] == f"{_HOST}/api/2.0/mcp/sql"
