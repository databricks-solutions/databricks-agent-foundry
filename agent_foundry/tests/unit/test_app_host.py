"""Tests for the generic App-host engine (issue #337).

``agent_foundry.deploy.app_host`` owns the brick-agnostic half of the
Databricks App lifecycle (spec reconcile, SP grants, code deploy,
tagging, teardown).  These tests pin the engine contract directly --
independent of any payload brick -- and guard the layering rule that the
framework engine must not import from ``bricks/*``.

Requires ``databricks-sdk``; skipped otherwise.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

pytest.importorskip("databricks.sdk", reason="databricks-sdk not installed")

from agent_foundry.deploy import app_host  # noqa: E402  # importorskip gate


@contextmanager
def _patch_wait_clock() -> Iterator[mock.Mock]:
    """Advance ``time.monotonic`` when ``time.sleep`` is called so wait loops finish."""
    now = 0.0
    slept = mock.Mock()

    def monotonic() -> float:
        return now

    def sleep(seconds: float) -> None:
        nonlocal now
        slept(seconds)
        now += max(seconds, 0.001)

    with (
        mock.patch("agent_foundry.deploy.app_host.time.monotonic", monotonic),
        mock.patch("agent_foundry.deploy.app_host.time.sleep", sleep),
    ):
        yield slept


# ---------------------------------------------------------------------------
# layering contract (#337): the engine stays payload-agnostic
# ---------------------------------------------------------------------------


class TestLayeringContract:
    def test_engine_does_not_import_bricks(self) -> None:
        """The framework engine must not depend on any payload brick."""
        source = Path(app_host.__file__).read_text(encoding="utf-8")
        assert "import bricks" not in source
        assert "from bricks" not in source


# ---------------------------------------------------------------------------
# app_exists
# ---------------------------------------------------------------------------


class TestAppExists:
    def test_typed_not_found_returns_false(self) -> None:
        from databricks.sdk.errors import NotFound

        ws = mock.MagicMock()
        ws.apps.get.side_effect = NotFound("App not found.")
        assert app_host.app_exists(ws, "missing-app") is False

    def test_legacy_substring_returns_false(self) -> None:
        ws = mock.MagicMock()
        ws.apps.get.side_effect = Exception("RESOURCE_DOES_NOT_EXIST")
        assert app_host.app_exists(ws, "missing-app") is False

    def test_existing_app_returns_true(self) -> None:
        ws = mock.MagicMock()
        ws.apps.get.return_value = mock.MagicMock()
        assert app_host.app_exists(ws, "real-app") is True

    def test_unexpected_error_returns_false_and_warns(self) -> None:
        ws = mock.MagicMock()
        ws.apps.get.side_effect = Exception("boom")
        assert app_host.app_exists(ws, "weird-app") is False


class TestFormatSdkError:
    def test_empty_oserror_uses_repr(self) -> None:
        text = app_host.format_sdk_error(OSError(None))
        assert "None" in text
        assert text != "None"

    def test_error_code_is_included(self) -> None:
        exc = RuntimeError("nope")
        exc.error_code = "RESOURCE_ALREADY_EXISTS"  # type: ignore[attr-defined]
        assert "RESOURCE_ALREADY_EXISTS" in app_host.format_sdk_error(exc)


class TestWaitUntilAppGone:
    def test_one_not_found_is_not_enough(self) -> None:
        from databricks.sdk.errors import NotFound

        ws = mock.MagicMock()
        lingering = mock.MagicMock()
        ws.apps.get.side_effect = [NotFound("gone"), lingering, lingering, lingering]
        now = 0.0

        def clock() -> float:
            return now

        def sleep(seconds: float) -> None:
            nonlocal now
            now += seconds

        gone = app_host.wait_until_app_gone(
            ws, "app", timeout_seconds=2.5, poll_seconds=1, gone_polls=3, sleep=sleep, clock=clock
        )
        assert gone is False

    def test_consecutive_not_found_counts_as_gone(self) -> None:
        from databricks.sdk.errors import NotFound

        ws = mock.MagicMock()
        ws.apps.get.side_effect = NotFound("gone")
        with _patch_wait_clock():
            gone = app_host.wait_until_app_gone(ws, "app", timeout_seconds=30, poll_seconds=0, gone_polls=3)
        assert gone is True
        assert ws.apps.get.call_count == 3


class TestCreateAppWithRetry:
    def test_retries_empty_error_then_succeeds(self) -> None:
        ws = mock.MagicMock()
        ws.apps.create_and_wait.side_effect = [OSError(None), None]
        slept: list[float] = []
        app_host.create_app_with_retry(ws, mock.MagicMock(), sleep=slept.append)
        assert ws.apps.create_and_wait.call_count == 2
        assert slept == [15]

    def test_permission_error_is_not_retried(self) -> None:
        ws = mock.MagicMock()
        ws.apps.create_and_wait.side_effect = RuntimeError("PERMISSION_DENIED")
        with pytest.raises(RuntimeError, match="PERMISSION_DENIED"):
            app_host.create_app_with_retry(ws, mock.MagicMock(), sleep=lambda _: None)
        ws.apps.create_and_wait.assert_called_once()


# ---------------------------------------------------------------------------
# reconcile_app_spec -- create vs async-update + dry-run
# ---------------------------------------------------------------------------


class TestReconcileAppSpec:
    def test_dry_run_makes_no_sdk_calls(self) -> None:
        ws = mock.MagicMock()
        app_host.reconcile_app_spec(None, "app", "desc", [], [], "medium", dry_run=True)
        ws.apps.create_and_wait.assert_not_called()
        ws.apps.create_update_and_wait.assert_not_called()

    def test_create_path_sets_compute_size(self) -> None:
        from databricks.sdk.service.apps import ComputeSize

        ws = mock.MagicMock()
        ws.apps.get.side_effect = Exception("RESOURCE_DOES_NOT_EXIST")
        app_host.reconcile_app_spec(ws, "app", "desc", [], [], "large", dry_run=False)
        app_arg = ws.apps.create_and_wait.call_args.args[0]
        assert app_arg.compute_size == ComputeSize.LARGE
        ws.apps.create_update_and_wait.assert_not_called()

    def test_update_path_resizes_via_async_api(self) -> None:
        from databricks.sdk.service.apps import ComputeSize

        ws = mock.MagicMock()
        existing = mock.MagicMock()
        existing.compute_status = None
        ws.apps.get.return_value = existing
        app_host.reconcile_app_spec(ws, "app", "desc", [], [], "large", dry_run=False)
        ws.apps.create_and_wait.assert_not_called()
        kwargs = ws.apps.create_update_and_wait.call_args.kwargs
        assert "compute_size" in kwargs["update_mask"]
        assert kwargs["app"].compute_size == ComputeSize.LARGE

    def test_deleting_app_waits_then_creates(self) -> None:
        from databricks.sdk.errors import NotFound

        ws = mock.MagicMock()
        deleting = mock.MagicMock()
        deleting.compute_status.state = "DELETING"
        gone = NotFound("gone")
        ws.apps.get.side_effect = [deleting, deleting, gone, gone, gone, gone]
        with _patch_wait_clock():
            app_host.reconcile_app_spec(ws, "app", "desc", [], [], "medium", dry_run=False)
        ws.apps.create_and_wait.assert_called_once()
        ws.apps.create_update_and_wait.assert_not_called()

    def test_create_retries_when_name_still_reserved(self) -> None:
        ws = mock.MagicMock()
        ws.apps.get.side_effect = Exception("RESOURCE_DOES_NOT_EXIST")
        ws.apps.create_and_wait.side_effect = [OSError(None), None]
        with _patch_wait_clock():
            app_host.reconcile_app_spec(ws, "app", "desc", [], [], "medium", dry_run=False)
        assert ws.apps.create_and_wait.call_count == 2


# ---------------------------------------------------------------------------
# telemetry export -- destination parsing + reconcile phase 3
# ---------------------------------------------------------------------------


def _existing_app_client() -> mock.MagicMock:
    ws = mock.MagicMock()
    ws.apps.get.return_value.compute_status = None
    return ws


class TestTelemetryExportDestinations:
    def test_none_yields_empty(self) -> None:
        assert app_host.telemetry_export_destinations(None) == []

    def test_unresolved_variables_yield_empty(self) -> None:
        assert app_host.telemetry_export_destinations("${var.tracing_destination}.${var.tracing_table_prefix}") == []

    def test_prefix_is_prepended_to_table_names(self) -> None:
        (dest,) = app_host.telemetry_export_destinations("cat.sch.mlflow_experiment")
        assert dest.unity_catalog is not None
        assert dest.unity_catalog.logs_table == "cat.sch.mlflow_experiment_otel_logs"
        assert dest.unity_catalog.metrics_table == "cat.sch.mlflow_experiment_otel_metrics"
        assert dest.unity_catalog.traces_table == "cat.sch.mlflow_experiment_otel_spans"

    def test_without_prefix_uses_bare_table_names(self) -> None:
        (dest,) = app_host.telemetry_export_destinations("cat.sch")
        assert dest.unity_catalog is not None
        assert dest.unity_catalog.logs_table == "cat.sch.otel_logs"

    @pytest.mark.parametrize("bad", ["cat", "cat..prefix", "a.b.c.d"])
    def test_malformed_destination_raises(self, bad: str) -> None:
        with pytest.raises(ValueError, match="telemetry_destination"):
            app_host.telemetry_export_destinations(bad)


class TestReconcileTelemetry:
    def test_no_destination_never_touches_telemetry(self) -> None:
        ws = _existing_app_client()
        app_host.reconcile_app_spec(ws, "app", "desc", [], [], "medium", dry_run=False)
        masks = [c.kwargs["update_mask"] for c in ws.apps.create_update_and_wait.call_args_list]
        assert all("telemetry_export_destinations" not in m for m in masks)

    def test_destination_applied_as_last_phase(self) -> None:
        ws = _existing_app_client()
        app_host.reconcile_app_spec(
            ws, "app", "desc", [], [], "medium", dry_run=False, telemetry_destination="cat.sch.pfx"
        )
        last = ws.apps.create_update_and_wait.call_args_list[-1].kwargs
        assert last["update_mask"] == "telemetry_export_destinations"
        (dest,) = last["app"].telemetry_export_destinations
        assert dest.unity_catalog.traces_table == "cat.sch.pfx_otel_spans"

    def test_telemetry_failure_does_not_raise(self) -> None:
        ws = _existing_app_client()
        ws.apps.create_update_and_wait.side_effect = [None, RuntimeError("PERMISSION_DENIED")]
        app_host.reconcile_app_spec(ws, "app", "desc", [], [], "medium", dry_run=False, telemetry_destination="cat.sch")
        assert ws.apps.create_update_and_wait.call_count == 2

    def test_malformed_destination_fails_before_sdk_calls(self) -> None:
        ws = _existing_app_client()
        with pytest.raises(ValueError):
            app_host.reconcile_app_spec(ws, "app", "desc", [], [], "medium", dry_run=False, telemetry_destination="cat")
        ws.apps.create_update_and_wait.assert_not_called()


# ---------------------------------------------------------------------------
# destroy_app -- generic teardown (reshaped in #337 to accept ws|None)
# ---------------------------------------------------------------------------


def _delete_makes_app_missing(ws: mock.MagicMock) -> None:
    def _delete(*, name: str) -> None:
        del name
        ws.apps.get.side_effect = Exception("RESOURCE_DOES_NOT_EXIST")

    ws.apps.delete.side_effect = _delete


class TestDestroyApp:
    def test_dry_run_returns_zero_without_client(self) -> None:
        rc = app_host.destroy_app(None, "app", solution_root="/sol", dry_run=True)
        assert rc == 0

    def test_missing_app_is_noop_success(self) -> None:
        ws = mock.MagicMock()
        ws.apps.get.side_effect = Exception("RESOURCE_DOES_NOT_EXIST")
        with mock.patch.object(app_host, "clean_workspace_dir") as clean:
            rc = app_host.destroy_app(ws, "app", solution_root="/sol", dry_run=False)
        assert rc == 0
        ws.apps.delete.assert_not_called()
        clean.assert_called_once()

    def test_missing_workspace_dir_is_not_reported_removed(self, capsys: pytest.CaptureFixture[str]) -> None:
        ws = mock.MagicMock()
        ws.apps.get.side_effect = Exception("RESOURCE_DOES_NOT_EXIST")
        with mock.patch.object(app_host, "clean_workspace_dir", return_value=False):
            app_host.destroy_app(ws, "app", solution_root="/sol", dry_run=False)
        assert "Removed workspace dir" not in capsys.readouterr().out

    def test_successful_delete_and_clean(self) -> None:
        ws = mock.MagicMock()
        ws.apps.get.return_value = mock.MagicMock()
        _delete_makes_app_missing(ws)
        with mock.patch.object(app_host, "clean_workspace_dir") as clean, _patch_wait_clock():
            rc = app_host.destroy_app(ws, "app", solution_root="/sol", dry_run=False)
        assert rc == 0
        ws.apps.delete.assert_called_once_with(name="app")
        clean.assert_called_once()

    def test_delete_waits_until_app_gone(self) -> None:
        ws = mock.MagicMock()
        lingering = mock.MagicMock()
        gets = iter([lingering, lingering, lingering])

        def _get(name: str) -> Any:
            del name
            try:
                return next(gets)
            except StopIteration as exc:
                raise Exception("RESOURCE_DOES_NOT_EXIST") from exc

        ws.apps.get.side_effect = _get
        with _patch_wait_clock() as slept:
            rc = app_host.destroy_app(ws, "app", solution_root="", dry_run=False, poll_seconds=1)
        assert rc == 0
        assert slept.call_count >= 3

    def test_delete_timeout_while_deleting_returns_one(self) -> None:
        ws = mock.MagicMock()
        ws.apps.get.return_value = mock.MagicMock()
        with mock.patch.object(app_host, "clean_workspace_dir") as clean:
            rc = app_host.destroy_app(ws, "app", solution_root="/sol", dry_run=False, timeout_seconds=0, poll_seconds=0)
        assert rc == 1
        ws.apps.delete.assert_called_once()
        clean.assert_not_called()

    def test_delete_failure_returns_one(self) -> None:
        ws = mock.MagicMock()
        ws.apps.get.return_value = mock.MagicMock()  # exists
        ws.apps.delete.side_effect = Exception("permission denied")
        with mock.patch.object(app_host, "clean_workspace_dir") as clean:
            rc = app_host.destroy_app(ws, "app", solution_root="/sol", dry_run=False)
        assert rc == 1
        clean.assert_not_called()  # bail before workspace cleanup

    def test_clean_failure_is_downgraded_to_warning(self) -> None:
        ws = mock.MagicMock()
        ws.apps.get.return_value = mock.MagicMock()
        _delete_makes_app_missing(ws)
        with (
            mock.patch.object(app_host, "clean_workspace_dir", side_effect=Exception("nope")),
            _patch_wait_clock(),
        ):
            rc = app_host.destroy_app(ws, "app", solution_root="/sol", dry_run=False)
        assert rc == 0  # App already deleted; stale dir is harmless

    def test_no_solution_root_skips_workspace_clean(self) -> None:
        ws = mock.MagicMock()
        ws.apps.get.return_value = mock.MagicMock()
        _delete_makes_app_missing(ws)
        with mock.patch.object(app_host, "clean_workspace_dir") as clean, _patch_wait_clock():
            rc = app_host.destroy_app(ws, "app", solution_root="", dry_run=False)
        assert rc == 0
        clean.assert_not_called()


# ---------------------------------------------------------------------------
# build_app_resources -- raw REST dict shaping (engine-owned)
# ---------------------------------------------------------------------------


class TestBuildAppResources:
    def test_skips_unresolved_placeholders(self) -> None:
        entries = [{"name": "llm", "serving_endpoint": {"name": "${var.unresolved}"}}]
        assert app_host.build_app_resources(entries) == []

    def test_keeps_resolved_entry(self) -> None:
        entries = [{"name": "llm", "serving_endpoint": {"name": "claude"}}]
        result = app_host.build_app_resources(entries)
        assert result == [{"name": "llm", "serving_endpoint": {"name": "claude"}}]

    def test_unknown_type_is_skipped(self) -> None:
        entries = [{"name": "mystery", "not_a_resource": {"id": "1"}}]
        assert app_host.build_app_resources(entries) == []

    def test_keeps_app_to_app_binding(self) -> None:
        entries = [{"name": "downstream", "app": {"name": "downstream-app", "permission": "CAN_USE"}}]
        assert app_host.build_app_resources(entries) == entries

    def test_keeps_database_binding(self) -> None:
        entries = [{"name": "lakebase", "database": {"database_name": "chat", "permission": "CAN_CONNECT_AND_CREATE"}}]
        assert app_host.build_app_resources(entries) == entries


class TestResourceTypeParity:
    """The hand-maintained key tuple must cover every SDK ``AppResource`` type."""

    def test_tuple_covers_every_sdk_app_resource_field(self) -> None:
        import dataclasses

        from databricks.sdk.service import apps

        sdk_types = {f.name for f in dataclasses.fields(apps.AppResource)} - {"name", "description"}
        missing = sdk_types - set(app_host._RESOURCE_TYPE_KEYS)
        assert not missing, f"SDK gained app resource type(s) {sorted(missing)}; add them to _RESOURCE_TYPE_KEYS"


# ---------------------------------------------------------------------------
# grant_sp_permissions -- GRANTs via the statement API
# ---------------------------------------------------------------------------

_CATALOG_GRANT = [{"securable_type": "CATALOG", "securable_name": "cat", "permissions": ["USE CATALOG"]}]


def _statement(state: str, message: str | None = None) -> mock.MagicMock:
    from databricks.sdk.service.sql import StatementState

    response = mock.MagicMock()
    response.status.state = StatementState(state)
    response.status.error = mock.MagicMock(message=message) if message else None
    return response


def _grant_ws(response: mock.MagicMock) -> mock.MagicMock:
    ws = mock.MagicMock()
    ws.apps.get.return_value = mock.MagicMock(service_principal_client_id="sp-1")
    ws.statement_execution.execute_statement.return_value = response
    return ws


class TestGrantSpPermissions:
    def test_succeeded_statement_reports_granted(self, capsys: pytest.CaptureFixture[str]) -> None:
        app_host.grant_sp_permissions(_grant_ws(_statement("SUCCEEDED")), "app", _CATALOG_GRANT, "wh", False)

        assert "Granted USE CATALOG on CATALOG cat to app SP" in capsys.readouterr().out

    def test_failed_statement_is_a_warning_not_granted(self, capsys: pytest.CaptureFixture[str]) -> None:
        ws = _grant_ws(_statement("FAILED", "PERMISSION_DENIED: User does not have MANAGE on Catalog 'cat'"))

        app_host.grant_sp_permissions(ws, "app", _CATALOG_GRANT, "wh", False)

        out = capsys.readouterr().out
        assert "Granted" not in out
        assert "WARNING: GRANT failed (USE CATALOG on cat): PERMISSION_DENIED" in out

    def test_already_granted_is_reported_as_such(self, capsys: pytest.CaptureFixture[str]) -> None:
        ws = _grant_ws(_statement("FAILED", "Principal already has USE CATALOG"))

        app_host.grant_sp_permissions(ws, "app", _CATALOG_GRANT, "wh", False)

        assert "USE CATALOG on cat: already granted" in capsys.readouterr().out

    def test_securable_name_is_back_quoted_in_the_statement(self, capsys: pytest.CaptureFixture[str]) -> None:
        grants = [{"securable_type": "SCHEMA", "securable_name": "my-cat.sch", "permissions": ["USE SCHEMA"]}]
        ws = _grant_ws(_statement("SUCCEEDED"))

        app_host.grant_sp_permissions(ws, "app", grants, "wh", False)

        statement = ws.statement_execution.execute_statement.call_args.kwargs["statement"]
        assert statement == "GRANT USE SCHEMA ON SCHEMA `my-cat`.`sch` TO `sp-1`"
        assert "Granted USE SCHEMA on SCHEMA my-cat.sch to app SP" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# App URL state (#337): fetch / publish / load / delete
# ---------------------------------------------------------------------------


class TestFetchAppUrl:
    def test_returns_url(self) -> None:
        ws = mock.MagicMock()
        ws.apps.get.return_value = mock.MagicMock(url="https://app.example.com")
        assert app_host.fetch_app_url(ws, "app") == "https://app.example.com"

    def test_missing_url_returns_none(self) -> None:
        ws = mock.MagicMock()
        ws.apps.get.return_value = mock.MagicMock(url=None)
        assert app_host.fetch_app_url(ws, "app") is None

    def test_error_returns_none(self) -> None:
        ws = mock.MagicMock()
        ws.apps.get.side_effect = Exception("boom")
        assert app_host.fetch_app_url(ws, "app") is None


class TestPublishAppState:
    def test_dry_run_persists_nothing(self) -> None:
        with mock.patch("agent_foundry.state.save_state") as save:
            result = app_host.publish_app_state(None, "inst", "dev", "app", dry_run=True)
        assert result == {}
        save.assert_not_called()

    def test_persists_url_to_dedicated_app_path(self) -> None:
        ws = mock.MagicMock()
        ws.apps.get.return_value = mock.MagicMock(url="https://app.example.com")
        with mock.patch("agent_foundry.state.save_state") as save:
            result = app_host.publish_app_state(ws, "inst", "dev", "my-app", deployer="me@co")
        assert result == {"app_name": "my-app", "app_url": "https://app.example.com"}
        args = save.call_args.args
        # save_state(ws, instance_name, brick_type, sub_instance, target, state)
        assert args[1] == "inst"
        assert args[2] == app_host.APP_STATE_BRICK_TYPE == "app"
        assert args[3] == "inst"
        assert args[4] == "dev"
        assert args[5]["app_url"] == "https://app.example.com"

    def test_missing_url_does_not_persist(self) -> None:
        ws = mock.MagicMock()
        ws.apps.get.return_value = mock.MagicMock(url=None)
        with mock.patch("agent_foundry.state.save_state") as save:
            result = app_host.publish_app_state(ws, "inst", "dev", "app")
        assert result == {}
        save.assert_not_called()


class TestLoadAppState:
    def test_empty_when_no_state(self) -> None:
        with mock.patch("agent_foundry.state.load_state", return_value={}):
            assert app_host.load_app_state(mock.MagicMock(), "inst", "dev") == {}

    def test_returns_validated_subset(self) -> None:
        raw = {"app_name": "my-app", "app_url": "https://x", "_audit": [{"action": "update"}]}
        with mock.patch("agent_foundry.state.load_state", return_value=raw):
            result = app_host.load_app_state(mock.MagicMock(), "inst", "dev")
        assert result == {"app_name": "my-app", "app_url": "https://x"}


class TestDeleteAppState:
    def test_delegates_to_state_delete(self) -> None:
        ws = mock.MagicMock()
        with mock.patch("agent_foundry.state.delete_state") as delete:
            app_host.delete_app_state(ws, "inst", "dev")
        args = delete.call_args.args
        assert args[1] == "inst"
        assert args[2] == "app"
        assert args[3] == "inst"
        assert args[4] == "dev"


# ---------------------------------------------------------------------------
# AppPayloadSpec -- the runtime-code seam (#337)
# ---------------------------------------------------------------------------


class TestAppPayloadSpec:
    def test_defaults_are_empty_runtime_only(self) -> None:
        spec = app_host.AppPayloadSpec()
        assert spec.command == []
        assert spec.env == []
        assert spec.requirements_extras == ("runtime",)

    def test_rejects_infrastructure_fields(self) -> None:
        """The payload is runtime-only; infra fields must be refused."""
        import pytest as _pytest
        from pydantic import ValidationError

        with _pytest.raises(ValidationError):
            app_host.AppPayloadSpec(compute_size="large")  # type: ignore[call-arg]

    def test_is_frozen(self) -> None:
        from pydantic import ValidationError

        spec = app_host.AppPayloadSpec(command=["python"])
        with pytest.raises(ValidationError):
            spec.command = ["other"]  # type: ignore[misc]


class TestRenderAppYaml:
    def test_empty_payload_renders_empty(self) -> None:
        assert app_host.render_app_yaml(app_host.AppPayloadSpec()) == {}

    def test_preserves_value_and_value_from_entries(self) -> None:
        spec = app_host.AppPayloadSpec(
            command=["python", "-m", "app"],
            env=[
                {"name": "A", "value": "1"},
                {"name": "B", "valueFrom": "secret-ref"},
            ],
        )
        rendered = app_host.render_app_yaml(spec)
        assert rendered["command"] == ["python", "-m", "app"]
        assert rendered["env"] == [
            {"name": "A", "value": "1"},
            {"name": "B", "valueFrom": "secret-ref"},
        ]


# ---------------------------------------------------------------------------
# Seam proof (#337): a non-agent payload drives the same engine
# ---------------------------------------------------------------------------


class _DummyRestPayload:
    """In-test stand-in for a future non-agent payload (e.g. a governed
    REST/MCP server).  It is NOT a shipped brick and is NOT registered --
    it exists only to prove the App-host engine is payload-agnostic: the
    same engine deploys a different ``AppPayloadSpec`` + neutral infra,
    importing nothing agent-specific.
    """

    def payload(self) -> Any:
        return app_host.AppPayloadSpec(
            command=["python", "-m", "my_rest_mcp"],
            env=[{"name": "MCP_MODE", "value": "governed"}],
            requirements_extras=("runtime",),
        )

    def deploy_on_host(self, ws: Any, app_name: str, workspace_path: str) -> None:
        # Pure engine calls -- the seam.  No agent module, no bricks/*.
        app_host.upload_app_yaml(ws, workspace_path, self.payload(), dry_run=False)
        app_host.reconcile_app_spec(ws, app_name, "governed rest payload", [], [], "large", dry_run=False)
        app_host.deploy_app_code(ws, app_name, workspace_path, dry_run=False)
        app_host.tag_app(ws, app_name, dry_run=False)


class TestPayloadSwapSeam:
    def test_non_agent_payload_rides_the_engine(self) -> None:
        import yaml
        from databricks.sdk.service.apps import ComputeSize

        ws = mock.MagicMock()
        ws.apps.get.return_value = mock.MagicMock()  # app exists -> update path

        _DummyRestPayload().deploy_on_host(ws, "rest-mcp-app", "/Shared/x/source")

        # 1. The engine uploaded *this payload's* app.yaml -- not the agent's.
        uploaded = ws.workspace.upload.call_args.kwargs
        body = yaml.safe_load(uploaded["content"].read().decode("utf-8"))
        assert body["command"] == ["python", "-m", "my_rest_mcp"]
        assert body["env"] == [{"name": "MCP_MODE", "value": "governed"}]

        # 2. The neutral infra (compute_size) flowed through the same spec reconcile.
        reconcile_kwargs = ws.apps.create_update_and_wait.call_args.kwargs
        assert reconcile_kwargs["app"].compute_size == ComputeSize.LARGE

        # 3. The same code-deploy + tag lifecycle ran for the swapped payload.
        ws.apps.deploy_and_wait.assert_called_once()


class TestUploadAppYaml:
    def test_dry_run_does_not_upload(self) -> None:
        ws = mock.MagicMock()
        spec = app_host.AppPayloadSpec(command=["python"])
        app_host.upload_app_yaml(None, "/ws/path", spec, dry_run=True)
        ws.workspace.upload.assert_not_called()

    def test_empty_payload_skips_upload(self) -> None:
        ws = mock.MagicMock()
        app_host.upload_app_yaml(ws, "/ws/path", app_host.AppPayloadSpec(), dry_run=False)
        ws.workspace.upload.assert_not_called()

    def test_uploads_rendered_yaml_with_value_from(self) -> None:
        import yaml

        ws = mock.MagicMock()
        spec = app_host.AppPayloadSpec(
            command=["python"],
            env=[{"name": "B", "valueFrom": "secret-ref"}],
        )
        app_host.upload_app_yaml(ws, "/ws/path", spec, dry_run=False)
        call = ws.workspace.upload.call_args
        assert call.kwargs["path"] == "/ws/path/app.yaml"
        body = call.kwargs["content"].read().decode("utf-8")
        loaded = yaml.safe_load(body)
        assert loaded["env"] == [{"name": "B", "valueFrom": "secret-ref"}]


# ---------------------------------------------------------------------------
# deploy_on_host / destroy_on_host -- the generic orchestrator (#338)
# ---------------------------------------------------------------------------


class _FakeRunnerPayload:
    """Minimal payload satisfying the adapter protocol for runner tests.

    It is NOT a registered adapter -- it exists only to drive
    ``deploy_on_host`` with no agent/bricks dependency, proving the
    orchestrator is payload-agnostic.
    """

    name = "fake"

    def __init__(self, recorder: Any) -> None:
        self._rec = recorder

    def applies_to(self, instance_dir: Path, config: Any) -> bool:  # noqa: ARG002
        return True

    def build_payload_spec(self, instance_dir: Any, variables: Any, capabilities: Any) -> Any:  # noqa: ARG002
        return app_host.AppPayloadSpec(command=["python", "-m", "fake"], requirements_extras=("runtime",))

    def build_infra(self, instance_dir: Any, variables: Any, *, target: str) -> Any:  # noqa: ARG002
        return app_host.AppInfraSpec(description="fake payload", compute_size="large")

    def after_reconcile(self, ws: Any, app_name: str, variables: Any, *, dry_run: bool) -> None:  # noqa: ARG002
        self._rec.after_reconcile(app_name=app_name, dry_run=dry_run)

    def provision(self, ctx: Any) -> int:  # noqa: ARG002
        return 0

    def plan_destroy(self, ctx: Any) -> list[Any]:  # noqa: ARG002
        return []

    def destroy(self, ctx: Any) -> list[Any]:  # noqa: ARG002
        return []

    def load_state_outputs(self, ctx: Any) -> dict[str, str]:  # noqa: ARG002
        return {}

    def scaffolds(self) -> tuple[Any, ...]:
        return ()


class TestDeployOnHost:
    def test_runs_generic_sequence_with_heal_hook(self, tmp_path: Path) -> None:
        ws = mock.MagicMock()
        rec = mock.Mock()
        manager = mock.Mock()
        payload = _FakeRunnerPayload(rec)
        instance_dir = tmp_path / "inst"
        instance_dir.mkdir()

        with mock.patch.multiple(
            app_host,
            sync_source=mock.DEFAULT,
            upload_app_yaml=mock.DEFAULT,
            reconcile_app_spec=mock.DEFAULT,
            grant_sp_permissions=mock.DEFAULT,
            deploy_app_code=mock.DEFAULT,
            tag_app=mock.DEFAULT,
            publish_app_state=mock.DEFAULT,
        ) as mocks:
            mocks["sync_source"].return_value = "/Workspace/x/source"
            for name, primitive in mocks.items():
                manager.attach_mock(primitive, name)
            manager.attach_mock(rec.after_reconcile, "after_reconcile")

            rc = app_host.deploy_on_host(
                payload,
                ws=ws,
                app_name="fake-app",
                target="dev",
                variables={"warehouse_id": "wh"},
                instance_dir=instance_dir,
                deployer="me@co.com",
                dry_run=False,
            )

        assert rc == 0
        order = [call[0] for call in manager.mock_calls]
        # the heal hook fires strictly between spec reconcile and code deploy
        assert order.index("reconcile_app_spec") < order.index("after_reconcile") < order.index("deploy_app_code")
        # the payload's runtime spec + neutral infra flowed into the engine
        assert mocks["upload_app_yaml"].call_args.args[2].command == ["python", "-m", "fake"]
        assert mocks["reconcile_app_spec"].call_args.args[5] == "large"
        assert "readme" in mocks["sync_source"].call_args.kwargs
        mocks["publish_app_state"].assert_called_once()

    def test_dry_run_invokes_heal_hook_without_client(self) -> None:
        rec = mock.Mock()
        payload = _FakeRunnerPayload(rec)
        rc = app_host.deploy_on_host(
            payload,
            ws=None,
            app_name="fake-app",
            target="dev",
            variables={},
            instance_dir=None,
            deployer="",
            dry_run=True,
        )
        assert rc == 0
        rec.after_reconcile.assert_called_once()


class TestResolvePayloadReadme:
    @staticmethod
    def _adapter_pkg(tmp_path: Path) -> Path:
        pkg = tmp_path / "adapter_pkg"
        pkg.mkdir()
        return pkg

    def test_instance_readme_wins(self, tmp_path: Path) -> None:
        pkg = self._adapter_pkg(tmp_path)
        (pkg / "README.md").write_text("framework", encoding="utf-8")
        instance_readme = tmp_path / "inst" / "payloads" / "fake" / "README.md"
        instance_readme.parent.mkdir(parents=True)
        instance_readme.write_text("instance", encoding="utf-8")

        with mock.patch("agent_foundry.deploy.app_host.inspect.getfile", return_value=str(pkg / "adapter.py")):
            resolved = app_host.resolve_payload_readme(_FakeRunnerPayload(mock.Mock()), tmp_path / "inst")

        assert resolved == instance_readme

    def test_falls_back_to_adapter_package_readme(self, tmp_path: Path) -> None:
        pkg = self._adapter_pkg(tmp_path)
        (pkg / "README.md").write_text("framework", encoding="utf-8")

        with mock.patch("agent_foundry.deploy.app_host.inspect.getfile", return_value=str(pkg / "adapter.py")):
            resolved = app_host.resolve_payload_readme(_FakeRunnerPayload(mock.Mock()), tmp_path / "inst")

        assert resolved == pkg / "README.md"

    def test_returns_none_without_any_readme(self, tmp_path: Path) -> None:
        pkg = self._adapter_pkg(tmp_path)

        with mock.patch("agent_foundry.deploy.app_host.inspect.getfile", return_value=str(pkg / "adapter.py")):
            resolved = app_host.resolve_payload_readme(_FakeRunnerPayload(mock.Mock()), tmp_path / "inst")

        assert resolved is None


class TestDestroyOnHost:
    def test_delegates_and_deletes_state(self, tmp_path: Path) -> None:
        ws = mock.MagicMock()
        instance_dir = tmp_path / "inst"
        instance_dir.mkdir()
        with mock.patch.multiple(app_host, destroy_app=mock.DEFAULT, delete_app_state=mock.DEFAULT) as mocks:
            mocks["destroy_app"].return_value = 0
            rc = app_host.destroy_on_host(ws, "fake-app", "dev", instance_dir=instance_dir, solution_root="/Shared/x")
        assert rc == 0
        mocks["destroy_app"].assert_called_once()
        mocks["delete_app_state"].assert_called_once_with(ws, "inst", "dev")

    def test_dry_run_skips_state_delete(self) -> None:
        with mock.patch.multiple(app_host, destroy_app=mock.DEFAULT, delete_app_state=mock.DEFAULT) as mocks:
            mocks["destroy_app"].return_value = 0
            rc = app_host.destroy_on_host(None, "fake-app", "dev", instance_dir=None, dry_run=True)
        assert rc == 0
        mocks["delete_app_state"].assert_not_called()
