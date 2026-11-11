"""Tests for ``AppBrick.plan_destroy`` / ``destroy`` (issue #338).

The App brick is a payload-driven host: teardown is the generic App
delete (delegated to ``deploy_agent_app.destroy``) plus the payload's
own teardown (``payload.plan_destroy`` / ``payload.destroy``).  For the
bundled ``agent`` payload that means the Lakebase chat-history project,
guarded by ``--force``.  SDK calls are mocked; state lives in an
in-memory workspace fake.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

pytest.importorskip("databricks.sdk", reason="databricks-sdk not installed")

from agent_foundry.bricks import DestroyContext  # noqa: E402
from agent_foundry.state import save_state  # noqa: E402
from agent_foundry.testing.fakes import FakeWorkspaceClient  # noqa: E402


def _write_chat_history_spec(instance_dir: Path, *, enabled: bool) -> None:
    """Author ``payloads/agent/payload.yml`` so destroy honors the spec."""
    payload_dir = instance_dir / "payloads" / "agent"
    payload_dir.mkdir(parents=True, exist_ok=True)
    flag = "true" if enabled else "false"
    (payload_dir / "payload.yml").write_text(
        f"payload:\n  adapter: agent\n  spec:\n    ui:\n      chat_history:\n        enabled: {flag}\n",
        encoding="utf-8",
    )


def _workspace_with_lakebase_state(instance_dir: Path, project: str = "dev-test-app-lakebase") -> FakeWorkspaceClient:
    state = {
        "lakebase_project_name": project,
        "lakebase_branch_path": f"projects/{project}/branches/production",
        "lakebase_database_path": f"projects/{project}/branches/production/databases/db-1",
    }
    ws = FakeWorkspaceClient()
    save_state(ws, instance_dir.name, "lakebase", instance_dir.name, "dev", state)
    return ws


def _ctx(
    instance_dir: Path,
    *,
    enabled: bool,
    force: bool,
    dry_run: bool = True,
    ws: FakeWorkspaceClient | None = None,
) -> DestroyContext:
    _write_chat_history_spec(instance_dir, enabled=enabled)
    return DestroyContext(
        instance_dir=instance_dir,
        target="dev",
        variables={"app_resource_name": "dev-test-app-app", "target_name": "dev"},
        dry_run=dry_run,
        ws=ws,
        config=None,
        force=force,
    )


class TestPlanDestroy:
    def test_includes_app_and_lakebase_when_enabled(self, tmp_path: Path) -> None:
        from bricks.app.brick import AppBrick

        inst = tmp_path / "inst"
        inst.mkdir()
        ws = _workspace_with_lakebase_state(inst)

        plan = AppBrick().plan_destroy(_ctx(inst, enabled=True, force=False, ws=ws))
        by_resource = {item.resource: item for item in plan.items}
        assert any(r.startswith("Databricks App") for r in by_resource)
        lakebase = next(item for item in plan.items if item.sub_instance == "lakebase")
        assert lakebase.destructive is True
        assert "Lakebase project" in lakebase.resource

    @mock.patch("payloads.agent.deploy.lakebase.destroy_lakebase_project", return_value=0)
    @mock.patch("bricks.app.scripts.deploy_agent_app.destroy", return_value=0)
    def test_plan_and_destroy_name_the_same_project(
        self, _mock_app_destroy: mock.MagicMock, _mock_lb_destroy: mock.MagicMock, tmp_path: Path
    ) -> None:
        from bricks.app.brick import AppBrick

        inst = tmp_path / "inst"
        inst.mkdir()
        ws = _workspace_with_lakebase_state(inst, "dev-my-app-lakebase")
        ctx = _ctx(inst, enabled=True, force=True, dry_run=False, ws=ws)

        plan = AppBrick().plan_destroy(ctx)
        deleted = [ref.resource_id for r in AppBrick().destroy(ctx) for ref in r.deleted]

        lakebase = next(item for item in plan.items if item.sub_instance == "lakebase")
        assert lakebase.resource == "Lakebase project dev-my-app-lakebase"
        assert "projects/dev-my-app-lakebase" in deleted

    def test_app_only_when_disabled(self, tmp_path: Path) -> None:
        from bricks.app.brick import AppBrick

        inst = tmp_path / "inst"
        inst.mkdir()
        ws = _workspace_with_lakebase_state(inst)

        plan = AppBrick().plan_destroy(_ctx(inst, enabled=False, force=False, ws=ws))
        assert len(plan.items) == 1
        assert plan.items[0].resource.startswith("Databricks App")
        assert plan.items[0].destructive is False


class TestDestroy:
    @mock.patch("payloads.agent.deploy.lakebase.destroy_lakebase_project")
    @mock.patch("bricks.app.scripts.deploy_agent_app.destroy", return_value=0)
    def test_lakebase_skipped_without_force(
        self, mock_app_destroy: mock.MagicMock, mock_lb_destroy: mock.MagicMock, tmp_path: Path
    ) -> None:
        from bricks.app.brick import AppBrick

        inst = tmp_path / "inst"
        inst.mkdir()
        ws = _workspace_with_lakebase_state(inst)

        results = AppBrick().destroy(_ctx(inst, enabled=True, force=False, ws=ws))

        mock_app_destroy.assert_called_once()
        mock_lb_destroy.assert_not_called()
        statuses = {r.name: r.status for r in results}
        assert statuses["dev-test-app-app"] == "ok"
        assert statuses["lakebase"] == "skipped"

    @mock.patch("payloads.agent.deploy.lakebase.destroy_lakebase_project", return_value=0)
    @mock.patch("bricks.app.scripts.deploy_agent_app.destroy", return_value=0)
    def test_lakebase_destroyed_with_force(
        self, mock_app_destroy: mock.MagicMock, mock_lb_destroy: mock.MagicMock, tmp_path: Path
    ) -> None:
        from bricks.app.brick import AppBrick

        inst = tmp_path / "inst"
        inst.mkdir()
        ws = _workspace_with_lakebase_state(inst)

        results = AppBrick().destroy(_ctx(inst, enabled=True, force=True, ws=ws))

        mock_app_destroy.assert_called_once()
        mock_lb_destroy.assert_called_once()
        statuses = {r.name: r.status for r in results}
        assert statuses["lakebase"] == "ok"
        assert all(not r.deleted for r in results)

    @mock.patch("payloads.agent.deploy.lakebase.destroy_lakebase_project", return_value=0)
    @mock.patch("bricks.app.scripts.deploy_agent_app.destroy", return_value=0)
    def test_real_destroy_reports_app_and_lakebase_project(
        self, _mock_app_destroy: mock.MagicMock, _mock_lb_destroy: mock.MagicMock, tmp_path: Path
    ) -> None:
        from bricks.app.brick import AppBrick

        inst = tmp_path / "inst"
        inst.mkdir()
        ws = _workspace_with_lakebase_state(inst)

        results = AppBrick().destroy(_ctx(inst, enabled=True, force=True, dry_run=False, ws=ws))

        assert [(ref.kind, ref.resource_id) for r in results for ref in r.deleted] == [
            ("app", "dev-test-app-app"),
            ("lakebase_project", "projects/dev-test-app-lakebase"),
        ]

    @mock.patch("payloads.agent.deploy.lakebase.destroy_lakebase_project")
    @mock.patch("bricks.app.scripts.deploy_agent_app.destroy", return_value=0)
    def test_app_only_when_chat_history_disabled(
        self, mock_app_destroy: mock.MagicMock, mock_lb_destroy: mock.MagicMock, tmp_path: Path
    ) -> None:
        from bricks.app.brick import AppBrick

        inst = tmp_path / "inst"
        inst.mkdir()
        ws = _workspace_with_lakebase_state(inst)

        results = AppBrick().destroy(_ctx(inst, enabled=False, force=True, ws=ws))

        mock_app_destroy.assert_called_once()
        mock_lb_destroy.assert_not_called()
        assert [r.name for r in results] == ["dev-test-app-app"]

    @mock.patch("agent_foundry.deploy.app_host.app_exists", return_value=False)
    @mock.patch("bricks.app.scripts.deploy_agent_app.destroy", return_value=0)
    def test_missing_app_is_skipped_but_still_cleaned_up(
        self, mock_app_destroy: mock.MagicMock, _mock_exists: mock.MagicMock, tmp_path: Path
    ) -> None:
        """No App reads ``[SKIP]``; destroy still runs for the workspace dir and URL state (#542)."""
        from agent_foundry.destroy_base import NOTHING_TO_DESTROY
        from bricks.app.brick import AppBrick

        inst = tmp_path / "inst"
        inst.mkdir()
        ctx = _ctx(inst, enabled=False, force=True, dry_run=False)
        ctx.ws = mock.MagicMock()

        results = AppBrick().destroy(ctx)

        mock_app_destroy.assert_called_once()
        assert [(r.status, r.details, r.deleted) for r in results] == [("skipped", {"reason": NOTHING_TO_DESTROY}, [])]
