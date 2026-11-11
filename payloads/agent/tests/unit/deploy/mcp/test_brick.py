"""Tests for ``payloads/agent/deploy/mcp/brick.py`` -- adapter + state outputs (issue #107)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from agent_foundry.bricks import DeployContext
from payloads.agent.deploy.mcp.brick import MCPBrick
from payloads.agent.tests.unit.deploy.mcp.conftest import FakeWorkspaceClient, write_server_yml


def _ctx(
    instance_dir: Path,
    variables: dict[str, str],
    *,
    ws: FakeWorkspaceClient | None = None,
    dry_run: bool = False,
) -> DeployContext:
    """Lightweight DeployContext for offline brick tests.

    Pass ``ws=fake_ws`` (from the ``fake_ws`` fixture) when the test
    needs ``load_state_outputs`` to round-trip workspace-persisted
    state.  Without ``ws`` the brick still runs but
    ``load_state_outputs`` returns ``{}`` (no local state path exists
    in the workspace-only contract).
    """
    return DeployContext(
        instance_dir=instance_dir,
        target="dev",
        variables=dict(variables),
        dry_run=dry_run,
        ws=ws,
    )


# ---------------------------------------------------------------------------
# applies_to: discovers the right shape of nested layout
# ---------------------------------------------------------------------------


class TestAppliesTo:
    def test_no_mcp_servers_dir_returns_false(self, tmp_path: Path) -> None:
        ctx = DeployContext(instance_dir=tmp_path, target="dev")
        assert MCPBrick().applies_to(ctx) is False

    def test_empty_mcp_servers_dir_returns_false(
        self,
        mcp_instance_dir: Path,
    ) -> None:
        ctx = DeployContext(instance_dir=mcp_instance_dir, target="dev")
        assert MCPBrick().applies_to(ctx) is False

    def test_mcp_servers_with_underscore_subdir_ignored(
        self,
        mcp_instance_dir: Path,
        mcp_servers_dir: Path,
    ) -> None:
        # ``_scaffold`` is a common pattern; brick must skip it.
        write_server_yml(
            mcp_servers_dir,
            "_scaffold",
            "type: managed_sql\ntools:\n  allow: ['x']\n",
        )
        ctx = DeployContext(instance_dir=mcp_instance_dir, target="dev")
        assert MCPBrick().applies_to(ctx) is False

    def test_mcp_servers_with_real_subdir_returns_true(
        self,
        mcp_instance_dir: Path,
        mcp_servers_dir: Path,
    ) -> None:
        write_server_yml(
            mcp_servers_dir,
            "sql",
            "type: managed_sql\ntools:\n  allow: ['x']\n",
        )
        ctx = DeployContext(instance_dir=mcp_instance_dir, target="dev")
        assert MCPBrick().applies_to(ctx) is True


# ---------------------------------------------------------------------------
# deploy: persists state per sub-instance to the workspace, dry-run does not
# ---------------------------------------------------------------------------


class TestDeploy:
    def test_persists_state_per_subdir_to_workspace(
        self,
        mcp_instance_dir: Path,
        mcp_servers_dir: Path,
        mcp_variables: dict[str, str],
        fake_ws: FakeWorkspaceClient,
    ) -> None:
        # Workspace is the single source of truth (issue #50): the
        # brick must call ``save_state`` (and therefore reach the
        # workspace) once per sub-instance, NOT write a local cache.
        write_server_yml(
            mcp_servers_dir,
            "sql",
            "type: managed_sql\ntools:\n  allow: ['exec_sql']\n",
        )
        write_server_yml(
            mcp_servers_dir,
            "github",
            "type: external\nconnection_name: github_mcp\ntools:\n  allow: ['search_repos']\n",
        )

        with patch(
            "payloads.agent.deploy.mcp.deploy._get_client",
            return_value=fake_ws,
        ):
            results = MCPBrick().deploy(
                _ctx(mcp_instance_dir, mcp_variables, ws=fake_ws),
            )

        assert {r.name for r in results} == {"sql", "github"}
        assert all(r.status == "ok" for r in results)

        # Workspace round-trip: state was uploaded under the canonical
        # path and can be read back.  No local _state directory exists
        # under either sub (issue #50: stale local copies must not be
        # bundled into deployed app source).
        for name in ("sql", "github"):
            ws_path = f"/Shared/agent-foundry/{mcp_instance_dir.name}/_state/mcp_servers/{name}/dev.json"
            assert ws_path in fake_ws._files, f"missing workspace state for {name}"
            local_dir = mcp_servers_dir / name / "_state"
            assert not local_dir.exists(), f"deploy() leaked a local _state directory for {name}"

    def test_dry_run_does_not_write_state(
        self,
        mcp_instance_dir: Path,
        mcp_servers_dir: Path,
        mcp_variables: dict[str, str],
    ) -> None:
        sub = write_server_yml(
            mcp_servers_dir,
            "sql",
            "type: managed_sql\ntools:\n  allow: ['x']\n",
        )
        results = MCPBrick().deploy(_ctx(mcp_instance_dir, mcp_variables, dry_run=True))
        assert all(r.status == "ok" for r in results)
        # No local _state file ever (the contract) and nothing should
        # touch the workspace either -- dry-run skips the client build.
        assert not (sub / "_state").exists()

    def test_validation_failure_marks_deploy_error(
        self,
        mcp_instance_dir: Path,
        mcp_servers_dir: Path,
        mcp_variables: dict[str, str],
        fake_ws: FakeWorkspaceClient,
    ) -> None:
        # Empty tools.allow -- the fail-closed rule.
        write_server_yml(
            mcp_servers_dir,
            "broken",
            "type: managed_sql\ntools:\n  allow: []\n",
        )
        with patch(
            "payloads.agent.deploy.mcp.deploy._get_client",
            return_value=fake_ws,
        ):
            results = MCPBrick().deploy(
                _ctx(mcp_instance_dir, mcp_variables, ws=fake_ws),
            )
        assert results and results[0].status == "deploy_error"

    def test_subdir_without_server_yml_skipped(
        self,
        mcp_instance_dir: Path,
        mcp_servers_dir: Path,
        mcp_variables: dict[str, str],
        fake_ws: FakeWorkspaceClient,
    ) -> None:
        # Real sub-dir with a server.yml -- should deploy.
        write_server_yml(
            mcp_servers_dir,
            "sql",
            "type: managed_sql\ntools:\n  allow: ['x']\n",
        )
        # A bare sub-dir with no server.yml -- should be ignored.
        (mcp_servers_dir / "stray").mkdir()

        with patch(
            "payloads.agent.deploy.mcp.deploy._get_client",
            return_value=fake_ws,
        ):
            results = MCPBrick().deploy(
                _ctx(mcp_instance_dir, mcp_variables, ws=fake_ws),
            )
        assert {r.name for r in results} == {"sql"}


# ---------------------------------------------------------------------------
# load_state_outputs: nested-layout convention publishes mcp__<sub>
# ---------------------------------------------------------------------------


class TestLoadStateOutputs:
    def test_emits_mcp_prefixed_var_per_sub(
        self,
        mcp_instance_dir: Path,
        mcp_servers_dir: Path,
        mcp_variables: dict[str, str],
        fake_ws: FakeWorkspaceClient,
    ) -> None:
        write_server_yml(
            mcp_servers_dir,
            "sql",
            "type: managed_sql\ntools:\n  allow: ['x']\n",
        )
        write_server_yml(
            mcp_servers_dir,
            "github",
            "type: external\nconnection_name: ghx\ntools:\n  allow: ['x']\n",
        )

        ctx = _ctx(mcp_instance_dir, mcp_variables, ws=fake_ws)
        with patch(
            "payloads.agent.deploy.mcp.deploy._get_client",
            return_value=fake_ws,
        ):
            MCPBrick().deploy(ctx)
        outputs = MCPBrick().load_state_outputs(ctx)

        assert outputs["mcp__sql"] == ("https://test.databricks.net/api/2.0/mcp/sql")
        assert outputs["mcp__github"] == ("https://test.databricks.net/api/2.0/mcp/external/ghx")

    def test_no_state_returns_empty(self, mcp_instance_dir: Path) -> None:
        outputs = MCPBrick().load_state_outputs(
            DeployContext(instance_dir=mcp_instance_dir, target="dev"),
        )
        assert outputs == {}

    def test_deploy_publishes_state_back_into_ctx_variables(
        self,
        mcp_instance_dir: Path,
        mcp_servers_dir: Path,
        mcp_variables: dict[str, str],
        fake_ws: FakeWorkspaceClient,
    ) -> None:
        """Mirrors the genie/vector_search pattern: ``ctx.variables`` is
        updated in-place so downstream bricks see fresh outputs."""
        write_server_yml(
            mcp_servers_dir,
            "sql",
            "type: managed_sql\ntools:\n  allow: ['x']\n",
        )
        ctx = _ctx(mcp_instance_dir, mcp_variables, ws=fake_ws)
        with patch(
            "payloads.agent.deploy.mcp.deploy._get_client",
            return_value=fake_ws,
        ):
            MCPBrick().deploy(ctx)
        assert "mcp__sql" in ctx.variables
