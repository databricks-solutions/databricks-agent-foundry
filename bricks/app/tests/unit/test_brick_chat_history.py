"""Integration tests for the Agent App brick's chat history capability.

Two paths exercised:

* ``chat_history.enabled = True``: ``AppBrick.deploy()`` calls
  ``provision_lakebase_project`` first, then the bundled app deploy.
* ``chat_history.enabled = False``: provisioning is skipped *and*
  the ``postgres`` ``app_resource`` is filtered out of the final
  resource list before the App is updated.

These tests do not hit the workspace -- all SDK calls are mocked.
The first-deploy ``NotFound`` regression guard for ``app_exists``
lives in ``test_deploy_agent_app.py::TestAppExistsHelper``; the
``clean_workspace_dir`` regression guard lives in
``agent_foundry/tests/unit/test_staging.py::TestCleanWorkspaceDir``.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from agent_foundry.bricks import DeployContext

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_chat_history_spec(instance_dir: Path, *, enabled: bool) -> None:
    """Author ``payloads/agent/payload.yml`` so the payload adapter
    seeds ``chat_history_enabled`` from the spec, not from a stale
    ``ctx.config.chat_history`` stand-in (issue #469).
    """
    payload_dir = instance_dir / "payloads" / "agent"
    payload_dir.mkdir(parents=True, exist_ok=True)
    flag = "true" if enabled else "false"
    (payload_dir / "payload.yml").write_text(
        f"payload:\n  adapter: agent\n  spec:\n    ui:\n      chat_history:\n        enabled: {flag}\n",
        encoding="utf-8",
    )


def _make_ctx(
    instance_dir: Path, *, enabled: bool, project_name_override: str = "", dry_run: bool = True
) -> DeployContext:
    """Build a hermetic ``DeployContext`` with an on-disk payload spec.

    Chat-history gating is owned by ``spec.ui.chat_history`` after
    #338; ``ctx.config`` is the framework ``InstanceConfig`` and is
    no longer read.  The variable bag is still populated so the
    bundled deploy filter sees a consistent key.
    """
    _write_chat_history_spec(instance_dir, enabled=enabled)
    return DeployContext(
        instance_dir=instance_dir,
        target="dev",
        variables={
            "app_resource_name": "dev-test-app-app",
            "app_name": "test-app",
            "target_name": "dev",
            "chat_history_enabled": "true" if enabled else "false",
        },
        dry_run=dry_run,
        ws=None,
        config=None,
    )


# ---------------------------------------------------------------------------
# AppBrick.deploy orchestration
# ---------------------------------------------------------------------------


class TestBrickProvisionsLakebaseWhenEnabled:
    """``AppBrick.deploy`` must call ``provision_lakebase_project``
    *before* the bundled app deploy when chat history is on, so the
    discovered lakebase paths are in ``ctx.variables`` by the time
    ``build_app_resources`` resolves the ``postgres`` template.
    """

    @mock.patch("bricks.app.scripts.deploy_agent_app.deploy")
    @mock.patch("payloads.agent.deploy.lakebase.provision_lakebase_project")
    def test_provision_called_in_order(
        self, mock_provision: mock.MagicMock, mock_deploy_app: mock.MagicMock, tmp_path: Path
    ) -> None:
        from bricks.app.brick import AppBrick

        mock_provision.return_value = 0
        mock_deploy_app.return_value = 0

        # Recording the call order proves provision runs first.
        manager = mock.Mock()
        manager.attach_mock(mock_provision, "provision")
        manager.attach_mock(mock_deploy_app, "app")

        ctx = _make_ctx(tmp_path / "inst", enabled=True)
        results = AppBrick().deploy(ctx)

        assert [c[0] for c in manager.mock_calls] == ["provision", "app"]
        assert len(results) == 1
        assert results[0].status == "ok"
        assert results[0].rc == 0

    @mock.patch("bricks.app.scripts.deploy_agent_app.deploy")
    @mock.patch("payloads.agent.deploy.lakebase.provision_lakebase_project")
    def test_provision_failure_aborts_app_deploy(
        self, mock_provision: mock.MagicMock, mock_deploy_app: mock.MagicMock, tmp_path: Path
    ) -> None:
        """If Lakebase provisioning returns non-zero, the bundled app
        deploy MUST NOT run -- the App would be bound to a postgres
        resource that does not exist yet, which is worse than
        surfacing the provisioning failure.
        """
        from bricks.app.brick import AppBrick

        mock_provision.return_value = 1

        ctx = _make_ctx(tmp_path / "inst", enabled=True)
        results = AppBrick().deploy(ctx)

        assert results[0].status == "deploy_error"
        assert results[0].rc == 1
        mock_deploy_app.assert_not_called()


class TestBrickSkipsLakebaseWhenDisabled:
    """``chat_history.enabled = False`` short-circuits provisioning."""

    @mock.patch("bricks.app.scripts.deploy_agent_app.deploy")
    @mock.patch("payloads.agent.deploy.lakebase.provision_lakebase_project")
    def test_provision_skipped(
        self, mock_provision: mock.MagicMock, mock_deploy_app: mock.MagicMock, tmp_path: Path
    ) -> None:
        from bricks.app.brick import AppBrick

        mock_deploy_app.return_value = 0

        ctx = _make_ctx(tmp_path / "inst", enabled=False)
        results = AppBrick().deploy(ctx)

        mock_provision.assert_not_called()
        mock_deploy_app.assert_called_once()
        assert results[0].status == "ok"

    @mock.patch("bricks.app.scripts.deploy_agent_app.deploy")
    @mock.patch("payloads.agent.deploy.lakebase.provision_lakebase_project")
    def test_skip_message_printed(
        self,
        mock_provision: mock.MagicMock,
        mock_deploy_app: mock.MagicMock,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The skip should be visible in the deploy log so operators do
        not wonder why their ``--- Lakebase ---`` block is empty."""
        from bricks.app.brick import AppBrick

        mock_deploy_app.return_value = 0

        ctx = _make_ctx(tmp_path / "inst", enabled=False)
        AppBrick().deploy(ctx)
        out = capsys.readouterr().out
        assert "Skipping Lakebase" in out
        assert "chat_history.enabled=false" in out


class TestBrickDefaultsOnWhenConfigMissing:
    """When ``ctx.config`` is ``None`` (legacy CLI path that fails to
    synthesise an ``InstanceConfig``), the brick falls back to the
    variable bag.  Absence of ``chat_history_enabled`` defaults to on
    -- matches the authoring contract.
    """

    @mock.patch("bricks.app.scripts.deploy_agent_app.deploy")
    @mock.patch("payloads.agent.deploy.lakebase.provision_lakebase_project")
    def test_fallback_to_variable_bag(
        self, mock_provision: mock.MagicMock, mock_deploy_app: mock.MagicMock, tmp_path: Path
    ) -> None:
        from bricks.app.brick import AppBrick

        mock_provision.return_value = 0
        mock_deploy_app.return_value = 0

        ctx = DeployContext(
            instance_dir=tmp_path / "inst",
            target="dev",
            # chat_history_enabled missing -> default-on branch.
            variables={
                "app_resource_name": "dev-test-app-app",
                "app_name": "test-app",
                "target_name": "dev",
            },
            dry_run=True,
            ws=None,
            config=None,
        )
        AppBrick().deploy(ctx)
        mock_provision.assert_called_once()


# ---------------------------------------------------------------------------
# postgres app_resource filtering inside the bundled deploy script
# ---------------------------------------------------------------------------


class TestPostgresResourceFilter:
    """``deploy_agent_app.deploy()`` drops the ``postgres`` entry
    from ``app_resources`` when ``chat_history_enabled != "true"``.
    The pre-filter avoids relying on the
    ``_has_unresolved_vars(...)`` heuristic to silently strip the
    entry -- with the explicit filter, opt-out deploys do not log
    "skipped due to unresolved ${var.lakebase_*}" warnings.
    """

    def test_postgres_dropped_when_disabled(self) -> None:
        from agent_foundry.deploy.app_host import build_app_resources
        from payloads.agent.contracts import AgentPayloadSpec
        from payloads.agent.deploy.config import filter_app_resources

        raw = [
            {"name": "llm", "serving_endpoint": {"name": "endpoint"}},
            {
                "name": "postgres",
                "postgres": {"branch": "b", "database": "d"},
            },
        ]
        # Chat history off in the spec drops postgres regardless of vars.
        spec = AgentPayloadSpec.model_validate({"ui": {"chat_history": {"enabled": False}}})
        filtered = filter_app_resources(raw, spec, {})
        resources = build_app_resources(filtered)
        names = [r["name"] for r in resources]
        assert "postgres" not in names
        assert "llm" in names

    def test_postgres_dropped_when_lakebase_database_missing(self) -> None:
        from agent_foundry.deploy.app_host import build_app_resources
        from payloads.agent.contracts import AgentPayloadSpec
        from payloads.agent.deploy.config import filter_app_resources

        raw = [
            {"name": "llm", "serving_endpoint": {"name": "endpoint"}},
            {
                "name": "postgres",
                "postgres": {
                    "branch": "projects/x/branches/production",
                    "database": "",
                    "permission": "CAN_CONNECT_AND_CREATE",
                },
            },
        ]
        # Chat history is on in the spec, but the Lakebase database path
        # was never discovered -> postgres is still dropped.
        spec = AgentPayloadSpec.model_validate({"ui": {"chat_history": {"enabled": True}}})
        filtered = filter_app_resources(
            raw,
            spec,
            {
                "lakebase_branch_path": "projects/x/branches/production",
                "lakebase_database_path": "",
            },
        )
        resources = build_app_resources(filtered)
        assert "postgres" not in [r["name"] for r in resources]

    def test_postgres_kept_when_enabled(self) -> None:
        from agent_foundry.deploy.app_host import build_app_resources

        raw = [
            {"name": "llm", "serving_endpoint": {"name": "endpoint"}},
            {
                "name": "postgres",
                "postgres": {
                    "branch": "projects/x/branches/production",
                    "database": "projects/x/branches/production/databases/db-1",
                    "permission": "CAN_CONNECT_AND_CREATE",
                },
            },
        ]
        resources = build_app_resources(raw)
        names = [r["name"] for r in resources]
        assert "postgres" in names
        assert "llm" in names
