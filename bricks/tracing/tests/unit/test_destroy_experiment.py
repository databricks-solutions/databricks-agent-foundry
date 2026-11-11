"""Tests for ``destroy_experiment`` in ``ensure_experiment.py``.

These tests guard against a regression we hit live: a too-broad
"not found" string match wiped local state for an experiment that
was still active in the workspace, then the next destroy run silently
reported [OK] because state was gone.  The fix tightens the
already-deleted detection and adds a tag-gated by-name fallback for
the missing-state case.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from bricks.tracing.scripts.ensure_experiment import (
    _execute_sql,
    _is_resource_does_not_exist,
    _prefixed_tables_in_schema,
    _schema_for_trace_tables,
    _sql_statement_state,
    _trace_destination,
    _trace_tables_from_experiment_tags,
    destroy_experiment,
)

_StateKey = tuple[str, str, str, str]


class _FakeStateStore:
    """In-memory stand-in for the workspace-backed state store (#338).

    The ``tracing`` brick persists experiment state to the workspace
    (single source of truth), never to a local file.  These offline
    tests swap ``agent_foundry.state.{load,save,delete}_state`` for this
    fake so we can seed / assert state without a workspace.
    """

    def __init__(self) -> None:
        self._state: dict[_StateKey, dict[str, Any]] = {}
        self.deleted: list[_StateKey] = []

    def load(
        self,
        ws: Any,
        instance_name: str,
        brick_type: str,
        sub_instance: str,
        target: str,
        *,
        instance_dir: Path | None = None,
    ) -> dict[str, Any]:
        return dict(self._state.get((instance_name, brick_type, sub_instance, target), {}))

    def save(
        self,
        ws: Any,
        instance_name: str,
        brick_type: str,
        sub_instance: str,
        target: str,
        state: dict[str, Any],
        **kwargs: Any,
    ) -> None:
        self._state[(instance_name, brick_type, sub_instance, target)] = dict(state)

    def delete(
        self,
        ws: Any,
        instance_name: str,
        brick_type: str,
        sub_instance: str,
        target: str,
        *,
        instance_dir: Path | None = None,
    ) -> None:
        key = (instance_name, brick_type, sub_instance, target)
        self.deleted.append(key)
        self._state.pop(key, None)

    def seed(self, instance_dir: Path, target: str, experiment_id: str) -> None:
        name = instance_dir.name
        self._state[(name, "tracing", name, target)] = {"experiment_id": experiment_id}

    def has_state(self, instance_dir: Path, target: str) -> bool:
        name = instance_dir.name
        return bool(self._state.get((name, "tracing", name, target)))


@pytest.fixture(autouse=True)
def state_store(monkeypatch: pytest.MonkeyPatch) -> _FakeStateStore:
    """Patch the workspace state store with the in-memory fake.

    ``destroy_experiment`` imports ``load_state`` / ``delete_state`` from
    ``agent_foundry.state`` at call time, so patching the module
    attributes is sufficient for every test in this module.
    """
    store = _FakeStateStore()
    monkeypatch.setattr("agent_foundry.state.load_state", store.load)
    monkeypatch.setattr("agent_foundry.state.save_state", store.save)
    monkeypatch.setattr("agent_foundry.state.delete_state", store.delete)
    return store


class _RestExc(Exception):
    """Stand-in for ``mlflow.exceptions.RestException``."""

    def __init__(self, message: str, error_code: str | None = None) -> None:
        super().__init__(message)
        if error_code is not None:
            self.error_code = error_code


class TestIsResourceDoesNotExist:
    def test_matches_error_code_attribute(self) -> None:
        exc = _RestExc("anything", error_code="RESOURCE_DOES_NOT_EXIST")
        assert _is_resource_does_not_exist(exc) is True

    def test_matches_message_substring(self) -> None:
        exc = RuntimeError("RESOURCE_DOES_NOT_EXIST: No Experiment with id=42 exists.")
        assert _is_resource_does_not_exist(exc) is True

    def test_does_not_match_generic_not_found_phrasing(self) -> None:
        """Critical: generic 'not found' must NOT be treated as success.

        That was the original bug -- broad substring matching wiped
        state for still-active experiments on transient errors.
        """
        assert _is_resource_does_not_exist(RuntimeError("connection not found")) is False
        assert _is_resource_does_not_exist(RuntimeError("file does not exist")) is False
        assert _is_resource_does_not_exist(RuntimeError("No Experiment with id=42 exists.")) is False


class _MlflowStubBase:
    """Base stub: every test needs ``set_tracking_uri`` to no-op so the
    helper's ``_bind_mlflow_to_databricks()`` call doesn't blow up."""

    def __init__(self) -> None:
        self.tracking_uri_calls: list[str] = []

    def set_tracking_uri(self, uri: str) -> None:
        self.tracking_uri_calls.append(uri)


class TestDestroyExperimentWithState:
    """Happy path + lifecycle handling when state holds the id."""

    def test_deletes_active_experiment_and_clears_state(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        state_store: _FakeStateStore,
    ) -> None:
        state_store.seed(tmp_path, "dev", "exp-1")

        calls: list[str] = []

        class _MlflowStub(_MlflowStubBase):
            def get_experiment(self, exp_id: str) -> Any:
                return SimpleNamespace(experiment_id=exp_id, lifecycle_stage="active")

            def delete_experiment(self, exp_id: str) -> None:
                calls.append(exp_id)

        stub = _MlflowStub()
        monkeypatch.setitem(__import__("sys").modules, "mlflow", stub)

        rc = destroy_experiment(tmp_path, "dev", variables={}, dry_run=False)
        assert rc == 0
        assert calls == ["exp-1"]
        assert not state_store.has_state(tmp_path, "dev")
        assert state_store.deleted, "Workspace state must be cleared after a successful destroy."
        assert "databricks" in stub.tracking_uri_calls, "Must bind MLflow to Databricks tracking server"

    def test_skips_already_deleted_lifecycle(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        state_store: _FakeStateStore,
    ) -> None:
        state_store.seed(tmp_path, "dev", "exp-1")

        class _MlflowStub(_MlflowStubBase):
            def get_experiment(self, exp_id: str) -> Any:
                return SimpleNamespace(experiment_id=exp_id, lifecycle_stage="deleted")

            def delete_experiment(self, exp_id: str) -> None:  # pragma: no cover - must not be called
                raise AssertionError("delete_experiment must not be called on already-deleted lifecycle")

        monkeypatch.setitem(__import__("sys").modules, "mlflow", _MlflowStub())

        rc = destroy_experiment(tmp_path, "dev", variables={}, dry_run=False)
        assert rc == 0
        assert not state_store.has_state(tmp_path, "dev")

    def test_resource_does_not_exist_on_get_is_success(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        state_store: _FakeStateStore,
    ) -> None:
        state_store.seed(tmp_path, "dev", "exp-1")

        class _MlflowStub(_MlflowStubBase):
            def get_experiment(self, exp_id: str) -> Any:  # noqa: ARG002
                raise _RestExc("No Experiment exists", error_code="RESOURCE_DOES_NOT_EXIST")

            def delete_experiment(self, exp_id: str) -> None:  # pragma: no cover
                raise AssertionError("delete_experiment must not be called when get raises NOT_EXIST")

        monkeypatch.setitem(__import__("sys").modules, "mlflow", _MlflowStub())

        rc = destroy_experiment(tmp_path, "dev", variables={}, dry_run=False)
        assert rc == 0
        assert not state_store.has_state(tmp_path, "dev")

    def test_transient_error_keeps_state_and_returns_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        state_store: _FakeStateStore,
    ) -> None:
        """A generic transient error must NOT wipe state.

        Regression guard for the live bug: a too-broad 'not found'
        substring match deleted state for an active experiment.
        """
        state_store.seed(tmp_path, "dev", "exp-1")

        class _MlflowStub(_MlflowStubBase):
            def get_experiment(self, exp_id: str) -> Any:  # noqa: ARG002
                raise RuntimeError("Connection reset by peer")

            def delete_experiment(self, exp_id: str) -> None:  # pragma: no cover
                raise AssertionError("delete_experiment must not be reached")

        monkeypatch.setitem(__import__("sys").modules, "mlflow", _MlflowStub())

        rc = destroy_experiment(tmp_path, "dev", variables={}, dry_run=False)
        assert rc == 1
        assert state_store.has_state(tmp_path, "dev"), "Transient errors must preserve state for retry."
        assert not state_store.deleted, "Transient errors must not clear workspace state."


class TestDestroyExperimentByNameFallback:
    """When local state is missing, recover by name + tag match."""

    def _stub_mlflow(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        experiment: Any | None,
        delete_calls: list[str],
    ) -> None:
        class _MlflowStub(_MlflowStubBase):
            def get_experiment_by_name(self, name: str) -> Any:  # noqa: ARG002
                return experiment

            def get_experiment(self, exp_id: str) -> Any:  # noqa: ARG002
                return experiment

            def delete_experiment(self, exp_id: str) -> None:
                delete_calls.append(exp_id)

        monkeypatch.setitem(__import__("sys").modules, "mlflow", _MlflowStub())

    def test_recovers_when_tag_matches(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        delete_calls: list[str] = []
        experiment = SimpleNamespace(
            experiment_id="recovered-id",
            lifecycle_stage="active",
            tags={"ai_solution_managed_by": "test-agent"},
        )
        self._stub_mlflow(monkeypatch, experiment=experiment, delete_calls=delete_calls)

        rc = destroy_experiment(
            tmp_path,
            "dev",
            variables={"tracing_experiment": "/Users/x/test-agent", "app_name": "test-agent"},
            dry_run=False,
        )
        assert rc == 0
        assert delete_calls == ["recovered-id"]
        assert "Recovered experiment id recovered-id" in capsys.readouterr().out

    def test_refuses_when_tag_mismatch(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Safety: never delete an experiment that belongs to a different solution."""
        delete_calls: list[str] = []
        experiment = SimpleNamespace(
            experiment_id="someone-elses-id",
            lifecycle_stage="active",
            tags={"ai_solution_managed_by": "other-solution"},
        )
        self._stub_mlflow(monkeypatch, experiment=experiment, delete_calls=delete_calls)

        rc = destroy_experiment(
            tmp_path,
            "dev",
            variables={"tracing_experiment": "/Users/x/shared-name", "app_name": "test-agent"},
            dry_run=False,
        )
        assert rc == 0
        assert delete_calls == [], "Must not delete an experiment owned by another solution."
        out = capsys.readouterr().out
        assert "refusing to delete" in out
        assert "Nothing to destroy" in out

    def test_refuses_when_no_tag_present(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        delete_calls: list[str] = []
        experiment = SimpleNamespace(
            experiment_id="untagged-id",
            lifecycle_stage="active",
            tags={},
        )
        self._stub_mlflow(monkeypatch, experiment=experiment, delete_calls=delete_calls)

        rc = destroy_experiment(
            tmp_path,
            "dev",
            variables={"tracing_experiment": "/Users/x/untagged", "app_name": "test-agent"},
            dry_run=False,
        )
        assert rc == 0
        assert delete_calls == []

    def test_no_experiment_name_no_fallback(
        self,
        tmp_path: Path,
    ) -> None:
        rc = destroy_experiment(tmp_path, "dev", variables={}, dry_run=False)
        assert rc == 0

    def test_binds_tracking_uri_before_get_experiment_by_name(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Regression: without ``set_tracking_uri('databricks')`` the
        helper hits the local default store and returns ``None`` for a
        workspace experiment.  This test asserts the URI is bound
        *before* the by-name lookup call.
        """
        delete_calls: list[str] = []
        order: list[str] = []
        experiment = SimpleNamespace(
            experiment_id="recovered-id",
            lifecycle_stage="active",
            tags={"ai_solution_managed_by": "test-agent"},
        )

        class _MlflowStub(_MlflowStubBase):
            def set_tracking_uri(self, uri: str) -> None:
                order.append(f"set_tracking_uri:{uri}")
                super().set_tracking_uri(uri)

            def get_experiment_by_name(self, name: str) -> Any:  # noqa: ARG002
                order.append("get_experiment_by_name")
                return experiment

            def get_experiment(self, exp_id: str) -> Any:  # noqa: ARG002
                order.append("get_experiment")
                return experiment

            def delete_experiment(self, exp_id: str) -> None:
                order.append("delete_experiment")
                delete_calls.append(exp_id)

        monkeypatch.setitem(__import__("sys").modules, "mlflow", _MlflowStub())

        rc = destroy_experiment(
            tmp_path,
            "dev",
            variables={
                "tracing_experiment": "/Users/x/test-agent",
                "app_name": "test-agent",
            },
            dry_run=False,
        )
        assert rc == 0
        assert delete_calls == ["recovered-id"]
        assert order[0] == "set_tracking_uri:databricks", (
            f"set_tracking_uri must be called BEFORE get_experiment_by_name. Order was: {order}"
        )
        assert order.index("set_tracking_uri:databricks") < order.index("get_experiment_by_name")


# ---------------------------------------------------------------------------
# Trace table teardown
# ---------------------------------------------------------------------------


_EXP_TAGS = {
    "ai_solution_managed_by": "test-agent",
    "mlflow.experiment.databricksTraceLogStorageTable": "cat.sch.mlflow_experiment_otel_logs",
    "mlflow.experiment.databricksTraceSpanStorageTable": "cat.sch.mlflow_experiment_otel_spans",
    "mlflow.experiment.databricksTraceAnnotationStorageTable": "cat.sch.mlflow_experiment_otel_annotations",
    "mlflow.experiment.databricksTraceStorageTable": "cat.sch.mlflow_experiment_trace_unified",
    "mlflow.experiment.databricksTraceDestinationPath": "cat.sch.mlflow_experiment",
    "mlflow.ownerEmail": "x@y",
}


class TestTraceTablesFromExperimentTags:
    def test_extracts_all_four_table_tags(self) -> None:
        tables = _trace_tables_from_experiment_tags(_EXP_TAGS)
        assert tables == [
            "cat.sch.mlflow_experiment_otel_logs",
            "cat.sch.mlflow_experiment_otel_spans",
            "cat.sch.mlflow_experiment_otel_annotations",
            "cat.sch.mlflow_experiment_trace_unified",
        ]

    def test_returns_empty_list_when_no_tags(self) -> None:
        assert _trace_tables_from_experiment_tags({}) == []

    def test_skips_missing_or_empty_tag_values(self) -> None:
        tags = dict(_EXP_TAGS)
        tags["mlflow.experiment.databricksTraceLogStorageTable"] = ""
        del tags["mlflow.experiment.databricksTraceStorageTable"]
        tables = _trace_tables_from_experiment_tags(tags)
        assert "cat.sch.mlflow_experiment_otel_logs" not in tables
        assert "cat.sch.mlflow_experiment_trace_unified" not in tables
        assert "cat.sch.mlflow_experiment_otel_spans" in tables


class TestSchemaForTraceTables:
    def test_single_schema(self) -> None:
        assert _schema_for_trace_tables(["cat.sch.t1", "cat.sch.t2"]) == "cat.sch"

    def test_mixed_schemas_returns_empty(self) -> None:
        """Safety: if tables live in different schemas, we won't drop any one."""
        assert _schema_for_trace_tables(["cat.sch1.t1", "cat.sch2.t2"]) == ""

    def test_empty_list(self) -> None:
        assert _schema_for_trace_tables([]) == ""

    def test_malformed_fqn_ignored(self) -> None:
        assert _schema_for_trace_tables(["just.two"]) == ""


class TestDestroyExperimentDropsTraceTables:
    """End-to-end: experiment delete with ``drop_trace_tables=True``
    must drop the tables enumerated by the experiment's tags and then
    attempt to drop the (now-empty) trace schema.
    """

    def _stub_mlflow_with_tags(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tags: dict[str, str],
    ) -> None:
        class _Stub(_MlflowStubBase):
            def get_experiment(self, exp_id: str) -> Any:
                return SimpleNamespace(
                    experiment_id=exp_id,
                    lifecycle_stage="active",
                    tags=tags,
                )

            def delete_experiment(self, exp_id: str) -> None:  # noqa: ARG002
                return None

        monkeypatch.setitem(__import__("sys").modules, "mlflow", _Stub())

    def _stub_workspace_sql(
        self,
        monkeypatch: pytest.MonkeyPatch,
        warehouse_id: str = "wh-test",
    ) -> tuple[list[str], object]:
        executed: list[str] = []

        class _ApiClient:
            def do(self, method: str, path: str, *, body: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG002
                executed.append(body["statement"])
                return {}

        class _Client:
            api_client = _ApiClient()

        monkeypatch.setattr(
            "agent_foundry.destroy_base.pick_any_running_warehouse",
            lambda ws: warehouse_id,  # noqa: ARG005
        )
        return executed, _Client()

    def test_drops_all_four_tables_and_schema(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        state_store: _FakeStateStore,
    ) -> None:
        state_store.seed(tmp_path, "dev", "exp-99")
        self._stub_mlflow_with_tags(monkeypatch, _EXP_TAGS)
        executed, client = self._stub_workspace_sql(monkeypatch)

        rc = destroy_experiment(
            tmp_path,
            "dev",
            variables={},
            dry_run=False,
            drop_trace_tables=True,
            ws=client,
        )
        assert rc == 0
        drops = [s for s in executed if s.startswith("DROP TABLE")]
        assert len(drops) == 4, f"Expected 4 DROP TABLE statements, got: {drops}"
        for fqn in (
            "mlflow_experiment_otel_logs",
            "mlflow_experiment_otel_spans",
            "mlflow_experiment_otel_annotations",
            "mlflow_experiment_trace_unified",
        ):
            assert any(fqn in s for s in drops), f"Missing DROP for {fqn}"
        schema_drops = [s for s in executed if s.startswith("DROP SCHEMA")]
        assert len(schema_drops) == 1
        assert "RESTRICT" in schema_drops[0], "Schema drop must be RESTRICT, not CASCADE"
        assert "`cat`.`sch`" in schema_drops[0]

    def test_skipped_when_drop_trace_tables_false(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        state_store: _FakeStateStore,
    ) -> None:
        """Default destroy (no --force) must NOT touch trace tables."""
        state_store.seed(tmp_path, "dev", "exp-99")
        self._stub_mlflow_with_tags(monkeypatch, _EXP_TAGS)
        executed, client = self._stub_workspace_sql(monkeypatch)

        rc = destroy_experiment(
            tmp_path,
            "dev",
            variables={},
            dry_run=False,
            drop_trace_tables=False,
            ws=client,
        )
        assert rc == 0
        assert executed == [], "Trace tables must only be dropped under --force"

    def test_by_name_fallback_also_drops_trace_tables(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The recovery path (state missing, found by name+tag) must
        also clean up trace tables -- otherwise we leak exactly what
        the user saw in the live test-agent destroy.
        """

        class _Stub(_MlflowStubBase):
            def get_experiment_by_name(self, name: str) -> Any:  # noqa: ARG002
                return SimpleNamespace(
                    experiment_id="recovered-id",
                    lifecycle_stage="active",
                    tags=_EXP_TAGS,
                )

            def get_experiment(self, exp_id: str) -> Any:  # noqa: ARG002
                return SimpleNamespace(
                    experiment_id="recovered-id",
                    lifecycle_stage="active",
                    tags=_EXP_TAGS,
                )

            def delete_experiment(self, exp_id: str) -> None:  # noqa: ARG002
                return None

        monkeypatch.setitem(__import__("sys").modules, "mlflow", _Stub())
        executed, client = self._stub_workspace_sql(monkeypatch)

        rc = destroy_experiment(
            tmp_path,
            "dev",
            variables={
                "tracing_experiment": "/Users/x/test-agent",
                "app_name": "test-agent",
            },
            dry_run=False,
            drop_trace_tables=True,
            ws=client,
        )
        assert rc == 0
        drops = [s for s in executed if s.startswith("DROP TABLE")]
        assert len(drops) == 4, f"By-name fallback must also drop trace tables. Got: {drops}"

    def test_schema_not_empty_warning_does_not_fail(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        state_store: _FakeStateStore,
    ) -> None:
        """If the schema isn't empty (RESTRICT fails), we log a friendly
        message and continue -- never fail the destroy."""
        state_store.seed(tmp_path, "dev", "exp-99")
        self._stub_mlflow_with_tags(monkeypatch, _EXP_TAGS)

        executed: list[str] = []

        class _ApiClient:
            def do(self, method: str, path: str, *, body: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG002
                stmt = body["statement"]
                executed.append(stmt)
                if stmt.startswith("DROP SCHEMA"):
                    raise RuntimeError("Schema cat.sch is not empty (RESTRICT)")
                return {}

        class _Client:
            api_client = _ApiClient()

        monkeypatch.setattr(
            "agent_foundry.destroy_base.pick_any_running_warehouse",
            lambda ws: "wh-test",  # noqa: ARG005
        )

        rc = destroy_experiment(
            tmp_path,
            "dev",
            variables={},
            dry_run=False,
            drop_trace_tables=True,
            ws=_Client(),
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "not empty" in out
        assert "preserved" in out


# ---------------------------------------------------------------------------
# Finding 2: MLflow auth env (destroy must pin the resolved profile/host)
#
# The helper itself now lives in ``agent_foundry.workspace_client`` and is
# unit-tested there (#421); what matters here is that destroy calls it
# before touching MLflow.
# ---------------------------------------------------------------------------


class TestDestroyExportsAuthEnv:
    """Regression for the live 401: destroy dereferenced MLflow's ambient
    default auth (ambiguous across shared-host profiles).  It must export
    the resolved profile/host from ``variables`` before any ``mlflow.*``.
    """

    def test_exports_before_delete(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        state_store: _FakeStateStore,
    ) -> None:
        monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)
        monkeypatch.delenv("DATABRICKS_HOST", raising=False)
        state_store.seed(tmp_path, "dev", "exp-1")

        seen_profile: dict[str, str] = {}

        class _MlflowStub(_MlflowStubBase):
            def get_experiment(self, exp_id: str) -> Any:
                seen_profile["at_get"] = os.environ.get("DATABRICKS_CONFIG_PROFILE", "")
                return SimpleNamespace(experiment_id=exp_id, lifecycle_stage="active", tags={})

            def delete_experiment(self, exp_id: str) -> None:  # noqa: ARG002
                return None

        monkeypatch.setitem(__import__("sys").modules, "mlflow", _MlflowStub())

        rc = destroy_experiment(
            tmp_path,
            "dev",
            variables={"profile": "dev-profile", "host": "https://ws.example"},
            dry_run=False,
        )
        assert rc == 0
        assert seen_profile["at_get"] == "dev-profile", "Profile must be exported before the MLflow lookup"
        assert os.environ.get("DATABRICKS_HOST") == "https://ws.example"

    def test_dry_run_does_not_touch_env(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        state_store: _FakeStateStore,
    ) -> None:
        monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)
        state_store.seed(tmp_path, "dev", "exp-1")

        rc = destroy_experiment(
            tmp_path,
            "dev",
            variables={"profile": "dev-profile", "host": "https://ws.example"},
            dry_run=True,
        )
        assert rc == 0
        assert "DATABRICKS_CONFIG_PROFILE" not in os.environ


# ---------------------------------------------------------------------------
# Finding 3b: SQL statement status must surface failures
# ---------------------------------------------------------------------------


class TestSqlStatementState:
    def test_extracts_failed_state_and_message(self) -> None:
        resp = {"status": {"state": "FAILED", "error": {"message": "boom"}}}
        assert _sql_statement_state(resp) == ("FAILED", "boom")

    def test_succeeded_no_error(self) -> None:
        assert _sql_statement_state({"status": {"state": "SUCCEEDED"}}) == ("SUCCEEDED", "")

    def test_missing_status_is_empty(self) -> None:
        assert _sql_statement_state({}) == ("", "")

    def test_non_dict_is_empty(self) -> None:
        assert _sql_statement_state(None) == ("", "")


class TestExecuteSql:
    def _client(self, response: Any) -> Any:
        class _ApiClient:
            def do(self, method: str, path: str, *, body: dict[str, Any]) -> Any:  # noqa: ARG002
                return response

        return SimpleNamespace(api_client=_ApiClient())

    def test_raises_on_failed_statement(self) -> None:
        client = self._client({"status": {"state": "FAILED", "error": {"message": "SCHEMA_NOT_EMPTY"}}})
        with pytest.raises(RuntimeError, match="SCHEMA_NOT_EMPTY"):
            _execute_sql(client, "wh-1", "DROP SCHEMA IF EXISTS `c`.`s` RESTRICT")

    def test_no_raise_on_succeeded(self) -> None:
        _execute_sql(self._client({"status": {"state": "SUCCEEDED"}}), "wh-1", "DROP TABLE t")

    def test_no_raise_on_empty_response(self) -> None:
        """Older clients / stubs return ``{}`` -- treated as success."""
        _execute_sql(self._client({}), "wh-1", "DROP TABLE t")


# ---------------------------------------------------------------------------
# Finding 3a: destination parsing + prefix enumeration of untagged tables
# ---------------------------------------------------------------------------


class TestTraceDestination:
    def test_prefers_destination_path_tag(self) -> None:
        assert _trace_destination(_EXP_TAGS, {}) == ("cat.sch", "mlflow_experiment")

    def test_falls_back_to_variables(self) -> None:
        got = _trace_destination(
            {},
            {"tracing_destination": "c.s", "tracing_table_prefix": "custom_prefix"},
        )
        assert got == ("c.s", "custom_prefix")

    def test_variables_default_prefix(self) -> None:
        assert _trace_destination({}, {"tracing_destination": "c.s"}) == ("c.s", "mlflow_experiment")

    def test_unknown_returns_empty(self) -> None:
        assert _trace_destination({}, {}) == ("", "")


def _table(name: str) -> Any:
    return SimpleNamespace(name=name)


class TestPrefixedTablesInSchema:
    def _client(self, names: list[str]) -> Any:
        class _Tables:
            def list(self, *, catalog_name: str, schema_name: str) -> list[Any]:  # noqa: ARG002
                return [_table(n) for n in names]

        return SimpleNamespace(tables=_Tables())

    def test_returns_only_prefixed_tables(self) -> None:
        client = self._client(
            [
                "mlflow_experiment_otel_logs",
                "mlflow_experiment_otel_metrics",
                "mlflow_experiment_trace_metadata",
                "format_byte_size",  # user object -- must be excluded
            ]
        )
        got = _prefixed_tables_in_schema(client, "cat.sch", "mlflow_experiment")
        assert got == [
            "cat.sch.mlflow_experiment_otel_logs",
            "cat.sch.mlflow_experiment_otel_metrics",
            "cat.sch.mlflow_experiment_trace_metadata",
        ]

    def test_listing_failure_returns_empty(self) -> None:
        class _Boom:
            def list(self, **_: Any) -> list[Any]:
                raise RuntimeError("no perms")

        got = _prefixed_tables_in_schema(SimpleNamespace(tables=_Boom()), "cat.sch", "mlflow_experiment")
        assert got == []

    def test_no_schema_returns_empty(self) -> None:
        assert _prefixed_tables_in_schema(SimpleNamespace(), "", "mlflow_experiment") == []


class TestDestroyDropsTaggedAndUntaggedTables:
    """The core of finding 3a: MLflow tags only 4 of its 6 trace tables.
    Destroy must also drop the untagged ``otel_metrics`` / ``trace_metadata``
    tables (enumerated by prefix) -- exactly what leaked live.
    """

    def _stub_mlflow(self, monkeypatch: pytest.MonkeyPatch, tags: dict[str, str]) -> None:
        class _Stub(_MlflowStubBase):
            def get_experiment(self, exp_id: str) -> Any:
                return SimpleNamespace(experiment_id=exp_id, lifecycle_stage="active", tags=tags)

            def delete_experiment(self, exp_id: str) -> None:  # noqa: ARG002
                return None

        monkeypatch.setitem(__import__("sys").modules, "mlflow", _Stub())

    def test_drops_untagged_metrics_and_metadata(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        state_store: _FakeStateStore,
    ) -> None:
        state_store.seed(tmp_path, "dev", "exp-99")
        self._stub_mlflow(monkeypatch, _EXP_TAGS)

        executed: list[str] = []
        schema_tables = [
            "mlflow_experiment_otel_logs",
            "mlflow_experiment_otel_spans",
            "mlflow_experiment_otel_annotations",
            "mlflow_experiment_trace_unified",
            "mlflow_experiment_otel_metrics",  # untagged
            "mlflow_experiment_trace_metadata",  # untagged
            "format_byte_size",  # user function-adjacent table -- must survive
        ]

        class _ApiClient:
            def do(self, method: str, path: str, *, body: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG002
                executed.append(body["statement"])
                return {"status": {"state": "SUCCEEDED"}}

        class _Tables:
            def list(self, *, catalog_name: str, schema_name: str) -> list[Any]:  # noqa: ARG002
                return [_table(n) for n in schema_tables]

        client = SimpleNamespace(api_client=_ApiClient(), tables=_Tables())
        monkeypatch.setattr(
            "agent_foundry.destroy_base.pick_any_running_warehouse",
            lambda ws: "wh-test",  # noqa: ARG005
        )

        rc = destroy_experiment(tmp_path, "dev", variables={}, dry_run=False, drop_trace_tables=True, ws=client)
        assert rc == 0
        drops = [s for s in executed if s.startswith("DROP TABLE")]
        assert len(drops) == 6, f"Expected all 6 trace tables dropped, got: {drops}"
        for suffix in ("otel_metrics", "trace_metadata"):
            assert any(suffix in s for s in drops), f"Untagged {suffix} table must be dropped"
        assert not any("format_byte_size" in s for s in drops), "Must not drop user objects"


class _RecordingDestroy:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, *_args: Any, **kwargs: Any) -> int:
        self.calls.append(kwargs)
        return 0


class TestBrickDestroy:
    """The brick resolves the experiment once and reports ``[SKIP]`` when there is none (#542)."""

    def test_resolved_id_is_passed_to_destroy(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import bricks.tracing.scripts.ensure_experiment as mod
        from agent_foundry.bricks import DestroyContext
        from bricks.tracing.brick import MlflowExperimentBrick

        destroy = _RecordingDestroy()
        monkeypatch.setattr(mod, "resolve_experiment_id", lambda *_a, **_k: "exp-9")
        monkeypatch.setattr(mod, "destroy_experiment", destroy)
        ctx = DestroyContext(instance_dir=tmp_path, target="dev", force=True)

        results = MlflowExperimentBrick().destroy(ctx)

        assert [call["experiment_id"] for call in destroy.calls] == ["exp-9"]
        assert [(r.status, [ref.resource_id for ref in r.deleted]) for r in results] == [("ok", ["exp-9"])]
