"""Tests for ``build_canonical_url`` (issue #107).

Golden URLs per type, plus the variable-substitution path through the
deploy script. Uses model construction directly rather than going via
YAML to keep the URL-formatting concern isolated.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from agent_foundry._instance_layout import resolve_payload_subdir
from agent_foundry.contracts.mcp import (
    CustomAppServerConfig,
    ExternalServerConfig,
    ManagedFunctionsServerConfig,
    ManagedGenieServerConfig,
    ManagedSqlServerConfig,
    ManagedVectorSearchServerConfig,
    ServerConfig,
    ToolsConfig,
)
from payloads.agent.deploy.mcp import deploy as deploy_mod
from payloads.agent.deploy.mcp.deploy import build_canonical_url, deploy
from payloads.agent.tests.unit.deploy.mcp.conftest import FakeWorkspaceClient, write_server_yml

_HOST = "https://test.databricks.net"
_TOOLS = ToolsConfig(allow=["x"])


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        (
            ManagedFunctionsServerConfig(
                catalog="cat",
                schema="sch",
                tools=_TOOLS,
            ),
            f"{_HOST}/api/2.0/mcp/functions/cat/sch",
        ),
        (
            ManagedGenieServerConfig(space_id="sgs_42", tools=_TOOLS),
            f"{_HOST}/api/2.0/mcp/genie/sgs_42",
        ),
        (
            ManagedVectorSearchServerConfig(
                catalog="cat",
                schema="sch",
                tools=_TOOLS,
            ),
            f"{_HOST}/api/2.0/mcp/vector-search/cat/sch",
        ),
        (
            ManagedSqlServerConfig(tools=_TOOLS),
            f"{_HOST}/api/2.0/mcp/sql",
        ),
        (
            ExternalServerConfig(connection_name="github_mcp", tools=_TOOLS),
            f"{_HOST}/api/2.0/mcp/external/github_mcp",
        ),
        (
            CustomAppServerConfig(
                app_url="https://my.workspace.databricksapps.com",
                tools=_TOOLS,
            ),
            "https://my.workspace.databricksapps.com",
        ),
    ],
    ids=[
        "managed_functions",
        "managed_genie",
        "managed_vector_search",
        "managed_sql",
        "external",
        "custom_app",
    ],
)
def test_build_canonical_url(config: ServerConfig, expected: str) -> None:
    assert build_canonical_url(config, _HOST) == expected


def test_host_trailing_slash_is_normalised() -> None:
    cfg = ExternalServerConfig(connection_name="ghx", tools=_TOOLS)
    assert build_canonical_url(cfg, _HOST + "/") == f"{_HOST}/api/2.0/mcp/external/ghx"


def test_custom_app_strips_trailing_slash_only() -> None:
    cfg = CustomAppServerConfig(
        app_url="https://my.app.databricksapps.com/",
        tools=_TOOLS,
    )
    # custom_app ignores the host arg even when it's empty.
    assert build_canonical_url(cfg, host="") == "https://my.app.databricksapps.com"


def test_missing_host_raises_for_managed_types() -> None:
    cfg = ManagedSqlServerConfig(tools=_TOOLS)
    with pytest.raises(ValueError, match="workspace host required"):
        build_canonical_url(cfg, host="")


# ---------------------------------------------------------------------------
# End-to-end: variable substitution -> validate -> deploy -> state file
# ---------------------------------------------------------------------------


def _read_workspace_state(
    fake_ws: FakeWorkspaceClient,
    instance_dir: Path,
    sub_name: str,
) -> dict[str, Any]:
    """Decode the workspace state JSON ``deploy()`` just uploaded.

    Encapsulates the canonical workspace path so the assertions below
    stay focused on state content, not on path arithmetic.
    """
    import json

    ws_path = f"/Shared/agent-foundry/{instance_dir.name}/_state/mcp_servers/{sub_name}/dev.json"
    result: dict[str, Any] = json.loads(fake_ws._files[ws_path].decode("utf-8"))
    return result


def test_var_substitution_round_trip(
    mcp_instance_dir: Path,
    mcp_variables: dict[str, str],
    fake_ws: FakeWorkspaceClient,
) -> None:
    # End-to-end: variable substitution in server.yml -> validate ->
    # deploy -> state JSON in the *workspace* (no local cache, see
    # issue #50).  The fake workspace gives us an in-memory round-trip
    # of the canonical save_state -> _read_workspace_file path.
    sub = write_server_yml(
        resolve_payload_subdir(mcp_instance_dir, "mcp_servers"),
        "agent_functions",
        """
        type: managed_functions
        catalog: "${var.catalog}"
        schema: "${var.schema_prefix}_agent"
        tools:
          allow: ["lookup_status"]
        """,
    )

    with patch.object(deploy_mod, "_get_client", return_value=fake_ws):
        rc = deploy(sub, target="dev", variables=mcp_variables, dry_run=False)
    assert rc == 0

    state = _read_workspace_state(fake_ws, mcp_instance_dir, "agent_functions")
    assert state["url"] == (f"{_HOST}/api/2.0/mcp/functions/test_catalog/test_app_agent")
    assert state["type"] == "managed_functions"
    assert state["tools"]["allow"] == ["lookup_status"]
    # name_prefix defaults to ``<server_name>__``.
    assert state["name_prefix"] == "agent_functions__"
    # ``schema`` (alias) survives ``model_dump(by_alias=True)``.
    assert "prompts" in state and "resources" in state
    # No local _state directory -- the contract.
    assert not (sub / "_state").exists()


def test_explicit_name_prefix_overrides_default(
    mcp_instance_dir: Path,
    mcp_variables: dict[str, str],
    fake_ws: FakeWorkspaceClient,
) -> None:
    sub = write_server_yml(
        resolve_payload_subdir(mcp_instance_dir, "mcp_servers"),
        "agent_functions",
        """
        type: managed_sql
        tools:
          allow: ["x"]
          name_prefix: "sql_helper__"
        """,
    )
    with patch.object(deploy_mod, "_get_client", return_value=fake_ws):
        rc = deploy(sub, target="dev", variables=mcp_variables, dry_run=False)
    assert rc == 0

    state = _read_workspace_state(fake_ws, mcp_instance_dir, "agent_functions")
    assert state["name_prefix"] == "sql_helper__"
    assert not (sub / "_state").exists()
