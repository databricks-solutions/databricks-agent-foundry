"""Shared fixtures for the MCP brick tests (issue #107).

All MCP-touching tests use ``MagicMock`` for the live SDK clients per
the test fixture convention documented in issue #107. Integration
tests against a real workspace are gated behind the
``--run-live`` pytest marker (see ``agent_foundry.pytest_plugin``).

:class:`~agent_foundry.testing.fakes.FakeWorkspaceClient` is the
in-memory equivalent of a real ``WorkspaceClient`` that lets the deploy
script's :func:`agent_foundry.state.save_state` round-trip a state
payload back out via :func:`agent_foundry.state.load_state` -- needed
because the MCP brick now persists state to the workspace exclusively
(issue #50, no local ``_state/`` cache).
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from agent_foundry._instance_layout import resolve_payload_subdir
from agent_foundry.testing.fakes import FakeWorkspaceClient

__all__ = ["FakeWorkspaceClient", "write_server_yml"]


@pytest.fixture
def fake_ws() -> FakeWorkspaceClient:
    """A fresh in-memory workspace client for one test.

    Use in tests that need ``deploy()`` to actually write state and
    a downstream call to ``load_state_outputs`` to read it back -- the
    workspace-only persistence contract makes a real round-trip the
    only honest way to test the orchestration end to end.
    """
    return FakeWorkspaceClient()


# Standard variable bundle every test starts from. Mirrors the keys the
# orchestrator's ``InstanceConfig.to_variables()`` exposes so the tests
# stay representative of the runtime environment.
_BASE_VARIABLES: dict[str, str] = {
    "host": "https://test.databricks.net",
    "catalog": "test_catalog",
    "schema_prefix": "test_app",
    "agent_schema": "test_catalog.test_app_agent",
    "warehouse_id": "wh_123",
    "app_name": "test-app",
    "sales_genie_space_id": "sgs_123",
}


@pytest.fixture
def mcp_variables() -> dict[str, str]:
    return dict(_BASE_VARIABLES)


@pytest.fixture
def mcp_instance_dir(tmp_path: Path) -> Path:
    """Empty ``instances/<app>/`` shell -- tests add ``mcp_servers/`` themselves.

    Providers now live under the payload root
    (``payloads/agent/mcp_servers/``); the directory is seeded here so
    ``MCPBrick.applies_to`` / ``deploy`` resolve it via
    :func:`agent_foundry._instance_layout.resolve_payload_subdir`.
    """
    inst = tmp_path / "instances" / "demo"
    resolve_payload_subdir(inst, "mcp_servers").mkdir(parents=True)
    return inst


@pytest.fixture
def mcp_servers_dir(mcp_instance_dir: Path) -> Path:
    """The payload-rooted ``payloads/agent/mcp_servers/`` dir tests populate."""
    return resolve_payload_subdir(mcp_instance_dir, "mcp_servers")


def write_server_yml(parent: Path, name: str, body: str) -> Path:
    """Helper: create ``parent/name/server.yml`` with *body* (auto-dedented)."""
    sub = parent / name
    sub.mkdir(parents=True, exist_ok=True)
    (sub / "server.yml").write_text(dedent(body).lstrip(), encoding="utf-8")
    return sub
