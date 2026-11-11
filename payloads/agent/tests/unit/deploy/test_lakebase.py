"""Tests for the Lakebase provisioner that backs the Agent App's
``postgres`` ``app_resource`` (offline; state lives in an in-memory workspace fake).

Relocated from ``bricks/lakebase/tests/test_deploy_lakebase.py`` when
the standalone Lakebase brick was merged into the Agent App brick.
The function under test is :func:`provision_lakebase_project` (the
former ``deploy``); the ``argparse`` ``main()`` entrypoint was dropped
because the function is no longer reachable from the CLI -- it is
called from ``AppBrick.deploy()`` only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from agent_foundry import state as shared_state
from agent_foundry.testing.fakes import FakeWorkspaceClient
from payloads.agent.deploy.lakebase import (
    _discover_database_path,
    _get_app_sp_client_id,
    _save_state,
    destroy_lakebase_project,
    ensure_app_sp_lakebase_access,
    load_state,
    provision_lakebase_project,
)

# ---------------------------------------------------------------------------
# State persistence (shared workspace state, agent_foundry.state)
# ---------------------------------------------------------------------------


def _shared(ws: Any, target: str = "dev") -> dict[str, Any]:
    return shared_state.load_state(ws, "test-app", "lakebase", "test-app", target)


class TestSharedState:
    def test_state_roundtrip_is_workspace_backed(self, lb_instance_dir: Path) -> None:
        ws = FakeWorkspaceClient()
        assert load_state(ws, lb_instance_dir, "dev") == {}

        _save_state(ws, lb_instance_dir, "dev", {"lakebase_project_name": "proj", "sp_client_id": "sp-1"}, deployer="")

        assert _shared(ws)["lakebase_project_name"] == "proj"
        assert load_state(ws, lb_instance_dir, "dev")["sp_client_id"] == "sp-1"
        assert not (lb_instance_dir / "lakebase" / "_state").exists()

    def test_destroy_from_shared_state_on_a_fresh_machine(
        self, lb_instance_dir: Path, lb_variables: dict[str, str]
    ) -> None:
        ws = FakeWorkspaceClient()
        shared_state.save_state(ws, "test-app", "lakebase", "test-app", "dev", {"lakebase_project_name": "stored-proj"})
        postgres = MagicMock()
        ws.postgres = postgres  # type: ignore[attr-defined]

        assert destroy_lakebase_project(lb_instance_dir, "dev", lb_variables, ws=ws) == 0

        postgres.delete_project.assert_called_once_with(name="projects/stored-proj")
        assert _shared(ws) == {}


# ---------------------------------------------------------------------------
# Database discovery
# ---------------------------------------------------------------------------


class TestDiscoverDatabasePath:
    def test_returns_first_database(self) -> None:
        api = MagicMock()
        api.do.return_value = {
            "databases": [
                {"name": "projects/p/branches/production/databases/db-first"},
                {"name": "projects/p/branches/production/databases/db-second"},
            ]
        }
        result = _discover_database_path(api, "p")
        assert result == "projects/p/branches/production/databases/db-first"
        api.do.assert_called_once_with(
            "GET",
            "/api/2.0/postgres/projects/p/branches/production/databases",
        )

    def test_returns_none_when_empty(self) -> None:
        api = MagicMock()
        api.do.return_value = {"databases": []}
        assert _discover_database_path(api, "p") is None

    def test_returns_none_on_api_error(self) -> None:
        api = MagicMock()
        api.do.side_effect = RuntimeError("network error")
        assert _discover_database_path(api, "p") is None


# ---------------------------------------------------------------------------
# App SP lookup
# ---------------------------------------------------------------------------


class TestGetAppSpClientId:
    def test_returns_sp_client_id(self) -> None:
        ws = MagicMock()
        ws.apps.get.return_value = MagicMock(
            service_principal_client_id="sp-12345",
        )
        assert _get_app_sp_client_id(ws, "my-app") == "sp-12345"
        ws.apps.get.assert_called_once_with("my-app")

    def test_returns_none_when_app_not_found(self) -> None:
        ws = MagicMock()
        ws.apps.get.side_effect = Exception("NOT_FOUND")
        assert _get_app_sp_client_id(ws, "missing-app") is None

    def test_returns_none_when_sp_id_is_empty(self) -> None:
        ws = MagicMock()
        ws.apps.get.return_value = MagicMock(
            service_principal_client_id="",
        )
        assert _get_app_sp_client_id(ws, "my-app") is None


# ---------------------------------------------------------------------------
# provision_lakebase_project -- dry-run
# ---------------------------------------------------------------------------


class TestProvisionDryRun:
    def test_returns_zero(self, lb_instance_dir: Path, lb_variables: dict[str, str]) -> None:
        rc = provision_lakebase_project(
            lb_instance_dir,
            "dev",
            lb_variables,
            dry_run=True,
        )
        assert rc == 0

    def test_prints_project_name(
        self, lb_instance_dir: Path, lb_variables: dict[str, str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        provision_lakebase_project(
            lb_instance_dir,
            "dev",
            lb_variables,
            dry_run=True,
        )
        output = capsys.readouterr().out
        assert "dev-test-app-lakebase" in output

    def test_uses_explicit_project_name(
        self, lb_instance_dir: Path, lb_variables: dict[str, str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        lb_variables["lakebase_project_name"] = "custom-project"
        provision_lakebase_project(
            lb_instance_dir,
            "dev",
            lb_variables,
            dry_run=True,
        )
        output = capsys.readouterr().out
        assert "custom-project" in output


# ---------------------------------------------------------------------------
# provision_lakebase_project -- SP rotation detection (mocked workspace)
# ---------------------------------------------------------------------------


def _make_mock_ws(
    sp_client_id: str = "sp-new",
    db_path: str = "projects/p/branches/production/databases/db-new",
    previous_state: dict[str, str] | None = None,
) -> MagicMock:
    """Build a mock WorkspaceClient with the minimum surface for the
    provisioner, optionally seeded with *previous_state*."""
    ws = MagicMock()
    ws.workspace = FakeWorkspaceClient().workspace
    if previous_state is not None:
        shared_state.save_state(ws, "test-app", "lakebase", "test-app", "dev", dict(previous_state))
    # A healthy (not soft-deleted) project: delete_time is None.
    ws.postgres.get_project.return_value = MagicMock(delete_time=None)
    ws.apps.get.return_value = MagicMock(
        service_principal_client_id=sp_client_id,
    )
    ws.api_client.do.return_value = {
        "databases": [{"name": db_path}],
    }
    return ws


class TestSPRotationDetection:
    """Verify provision_lakebase_project detects SP changes and persists the
    new SP for the heal pass.
    """

    @patch("payloads.agent.deploy.lakebase._get_client")
    def test_no_rotation_when_sp_unchanged(
        self,
        mock_get_client: MagicMock,
        lb_instance_dir: Path,
        lb_variables: dict[str, str],
        lb_state_with_sp: dict[str, str],
    ) -> None:
        ws = _make_mock_ws(sp_client_id="aaaa-old-sp-id", previous_state=lb_state_with_sp)
        mock_get_client.return_value = ws

        rc = provision_lakebase_project(lb_instance_dir, "dev", lb_variables)
        assert rc == 0

        ws.postgres.delete_database.assert_not_called()

        state = load_state(ws, lb_instance_dir, "dev")
        assert state["sp_client_id"] == "aaaa-old-sp-id"

    @patch("payloads.agent.deploy.lakebase._get_client")
    def test_rotation_is_logged_and_never_destructive(
        self,
        mock_get_client: MagicMock,
        lb_instance_dir: Path,
        lb_variables: dict[str, str],
        lb_state_with_sp: dict[str, str],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """SP rotation is surfaced for diagnostics only.  The framework
        never deletes or recreates the database -- the Agent App's
        declared ``postgres`` resource handles SP-role provisioning
        inside the database when the App deploys.  Existing state
        (database path etc.) must survive and the new SP is persisted.
        """
        ws = _make_mock_ws(sp_client_id="bbbb-new-sp-id", previous_state=lb_state_with_sp)
        mock_get_client.return_value = ws

        rc = provision_lakebase_project(lb_instance_dir, "dev", lb_variables)
        assert rc == 0

        ws.postgres.delete_database.assert_not_called()
        ws.postgres.create_database.assert_not_called()

        out = capsys.readouterr().out
        assert "SP rotation detected" in out

        state = load_state(ws, lb_instance_dir, "dev")
        assert state["sp_client_id"] == "bbbb-new-sp-id"
        assert state["lakebase_database_path"]
        assert "/databases/" in state["lakebase_database_path"]

    @patch("payloads.agent.deploy.lakebase._get_client")
    def test_no_rotation_on_first_deploy(
        self, mock_get_client: MagicMock, lb_instance_dir: Path, lb_variables: dict[str, str]
    ) -> None:
        """First deploy has no previous state -- should not trigger reset."""
        ws = _make_mock_ws(sp_client_id="first-sp-id")
        mock_get_client.return_value = ws

        rc = provision_lakebase_project(lb_instance_dir, "dev", lb_variables)
        assert rc == 0

        ws.postgres.delete_database.assert_not_called()

        state = load_state(ws, lb_instance_dir, "dev")
        assert state["sp_client_id"] == "first-sp-id"

    @patch("payloads.agent.deploy.lakebase._get_client")
    def test_no_rotation_when_app_not_found(
        self,
        mock_get_client: MagicMock,
        lb_instance_dir: Path,
        lb_variables: dict[str, str],
        lb_state_with_sp: dict[str, str],
    ) -> None:
        """If the app doesn't exist yet, SP lookup returns None -- no reset."""
        ws = _make_mock_ws(previous_state=lb_state_with_sp)
        ws.apps.get.side_effect = Exception("NOT_FOUND")
        mock_get_client.return_value = ws

        rc = provision_lakebase_project(lb_instance_dir, "dev", lb_variables)
        assert rc == 0

        ws.postgres.delete_database.assert_not_called()

    @patch("payloads.agent.deploy.lakebase._get_client")
    def test_state_always_persists_sp(
        self, mock_get_client: MagicMock, lb_instance_dir: Path, lb_variables: dict[str, str]
    ) -> None:
        ws = _make_mock_ws(sp_client_id="persisted-sp")
        mock_get_client.return_value = ws

        provision_lakebase_project(lb_instance_dir, "dev", lb_variables)

        state = load_state(ws, lb_instance_dir, "dev")
        assert state["sp_client_id"] == "persisted-sp"
        assert state["lakebase_project_name"] == "dev-test-app-lakebase"
        assert "lakebase_branch_path" in state
        assert "lakebase_database_path" in state


# ---------------------------------------------------------------------------
# provision_lakebase_project -- project creation
# ---------------------------------------------------------------------------


class TestProvisionProjectCreation:
    @patch("payloads.agent.deploy.lakebase._get_client")
    def test_creates_project_when_not_found(
        self, mock_get_client: MagicMock, lb_instance_dir: Path, lb_variables: dict[str, str]
    ) -> None:
        ws = _make_mock_ws()
        ws.postgres.get_project.side_effect = Exception("does not exist")
        mock_op = MagicMock()
        ws.postgres.create_project.return_value = mock_op
        mock_get_client.return_value = ws

        rc = provision_lakebase_project(lb_instance_dir, "dev", lb_variables)
        assert rc == 0
        ws.postgres.create_project.assert_called_once()
        mock_op.wait.assert_called_once()

    @patch("payloads.agent.deploy.lakebase._get_client")
    def test_adopts_existing_project_on_race(
        self, mock_get_client: MagicMock, lb_instance_dir: Path, lb_variables: dict[str, str]
    ) -> None:
        ws = _make_mock_ws()
        ws.postgres.get_project.side_effect = Exception("not found")
        ws.postgres.create_project.side_effect = Exception("already exists")
        mock_get_client.return_value = ws

        rc = provision_lakebase_project(lb_instance_dir, "dev", lb_variables)
        assert rc == 0

    @patch("payloads.agent.deploy.lakebase._get_client")
    def test_returns_error_on_unexpected_exception(
        self, mock_get_client: MagicMock, lb_instance_dir: Path, lb_variables: dict[str, str]
    ) -> None:
        ws = _make_mock_ws()
        ws.postgres.get_project.side_effect = Exception("network timeout")
        mock_get_client.return_value = ws

        rc = provision_lakebase_project(lb_instance_dir, "dev", lb_variables)
        assert rc == 1


class TestProvisionUndeletesSoftDeletedProject:
    """A destroy soft-deletes the Lakebase project (``delete_time`` set,
    name reserved until a 7-day purge).  ``get_project`` still returns it,
    so a naive redeploy logs "exists", skips create, then fails database
    discovery and silently drops chat history.  Provision must restore
    (undelete) the soft-deleted project so destroy->redeploy is
    idempotent.
    """

    @patch("payloads.agent.deploy.lakebase._get_client")
    def test_undeletes_when_soft_deleted(
        self,
        mock_get_client: MagicMock,
        lb_instance_dir: Path,
        lb_variables: dict[str, str],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        ws = _make_mock_ws()
        ws.postgres.get_project.return_value = MagicMock(delete_time="2026-07-02T12:26:06Z")
        mock_get_client.return_value = ws

        rc = provision_lakebase_project(lb_instance_dir, "dev", lb_variables)
        assert rc == 0
        ws.postgres.undelete_project.assert_called_once_with(name="projects/dev-test-app-lakebase")
        ws.postgres.undelete_project.return_value.wait.assert_called_once()
        assert "restoring (undelete)" in capsys.readouterr().out
        ws.postgres.create_project.assert_not_called()

    @patch("payloads.agent.deploy.lakebase._get_client")
    def test_healthy_project_is_not_undeleted(
        self, mock_get_client: MagicMock, lb_instance_dir: Path, lb_variables: dict[str, str]
    ) -> None:
        ws = _make_mock_ws()  # delete_time=None
        mock_get_client.return_value = ws

        rc = provision_lakebase_project(lb_instance_dir, "dev", lb_variables)
        assert rc == 0
        ws.postgres.undelete_project.assert_not_called()

    @patch("payloads.agent.deploy.lakebase._get_client")
    def test_undelete_failure_returns_error(
        self, mock_get_client: MagicMock, lb_instance_dir: Path, lb_variables: dict[str, str]
    ) -> None:
        ws = _make_mock_ws()
        ws.postgres.get_project.return_value = MagicMock(delete_time="2026-07-02T12:26:06Z")
        ws.postgres.undelete_project.side_effect = Exception("cannot restore")
        mock_get_client.return_value = ws

        rc = provision_lakebase_project(lb_instance_dir, "dev", lb_variables)
        assert rc == 1


# ---------------------------------------------------------------------------
# ensure_app_sp_lakebase_access -- SP-rotation schema-access heal
# ---------------------------------------------------------------------------

_BRANCH = "projects/p/branches/production"
_SP = "sp-current"
_ROLE = f"{_BRANCH}/roles/dbrx-apps-{_SP}"


def _ws_with_role(membership: list[Any]) -> MagicMock:
    """Mock WorkspaceClient whose SP Lakebase role carries *membership*."""
    ws = MagicMock()
    ws.apps.get.return_value = MagicMock(service_principal_client_id=_SP)
    ws.postgres.get_role.return_value = MagicMock(status=MagicMock(membership_roles=membership))
    return ws


class TestEnsureAppSpLakebaseAccess:
    def test_grants_superuser_when_missing(self) -> None:
        from databricks.sdk.service.postgres import RoleMembershipRole

        ws = _ws_with_role(membership=[])
        ensure_app_sp_lakebase_access(ws, "my-app", {"lakebase_branch_path": _BRANCH})

        ws.postgres.get_role.assert_called_once_with(name=_ROLE)
        ws.postgres.update_role.assert_called_once()
        kwargs = ws.postgres.update_role.call_args.kwargs
        assert kwargs["name"] == _ROLE
        assert kwargs["role"].spec.membership_roles == [RoleMembershipRole.DATABRICKS_SUPERUSER]
        assert kwargs["update_mask"].ToJsonString() == "spec.membership_roles"
        ws.postgres.update_role.return_value.wait.assert_called_once()

    def test_idempotent_when_already_superuser(self) -> None:
        from databricks.sdk.service.postgres import RoleMembershipRole

        ws = _ws_with_role(membership=[RoleMembershipRole.DATABRICKS_SUPERUSER])
        ensure_app_sp_lakebase_access(ws, "my-app", {"lakebase_branch_path": _BRANCH})

        ws.postgres.update_role.assert_not_called()

    def test_skips_when_role_not_yet_materialised(self) -> None:
        ws = MagicMock()
        ws.apps.get.return_value = MagicMock(service_principal_client_id=_SP)
        ws.postgres.get_role.side_effect = Exception("NOT_FOUND")

        ensure_app_sp_lakebase_access(ws, "my-app", {"lakebase_branch_path": _BRANCH})

        ws.postgres.update_role.assert_not_called()

    def test_skips_when_no_sp_client_id(self) -> None:
        ws = MagicMock()
        ws.apps.get.return_value = MagicMock(service_principal_client_id="")

        ensure_app_sp_lakebase_access(ws, "my-app", {"lakebase_branch_path": _BRANCH})

        ws.postgres.get_role.assert_not_called()
        ws.postgres.update_role.assert_not_called()

    def test_noop_when_no_branch_path(self) -> None:
        ws = MagicMock()
        ensure_app_sp_lakebase_access(ws, "my-app", {})
        ws.apps.get.assert_not_called()

    def test_dry_run_makes_no_api_calls(self, capsys: pytest.CaptureFixture[str]) -> None:
        ensure_app_sp_lakebase_access(None, "my-app", {"lakebase_branch_path": _BRANCH}, dry_run=True)
        assert "DRY RUN" in capsys.readouterr().out

    def test_api_error_is_swallowed(self) -> None:
        ws = _ws_with_role(membership=[])
        ws.postgres.update_role.side_effect = Exception("403 PERMISSION_DENIED")

        # Must not raise -- the heal is best-effort.
        ensure_app_sp_lakebase_access(ws, "my-app", {"lakebase_branch_path": _BRANCH})
