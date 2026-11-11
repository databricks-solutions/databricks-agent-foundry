"""Destroy means gone: :mod:`agent_foundry.deploy.gone` and its orchestrator wiring."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from databricks.sdk.errors import NotFound, PermissionDenied
from databricks.sdk.errors.base import DatabricksError
from pydantic import BaseModel

from agent_foundry.bricks import (
    BrickDestroyDefaults,
    BrickResult,
    DeployContext,
    DestroyContext,
    ResourceRef,
    deleted_refs,
)
from agent_foundry.deploy import gone
from agent_foundry.deploy.gone import DEFAULT_TIMEOUT_SECONDS, verify_gone, wait_until_gone

_SERVICES = (
    "genie",
    "vector_search_indexes",
    "jobs",
    "schemas",
    "functions",
    "connections",
    "apps",
    "api_client",
    "warehouses",
    "lakeview",
    "experiments",
    "postgres",
    "ai_gateway",
    "serving_endpoints",
    "registered_models",
)


def _raise_not_found(*_args: Any, **_kwargs: Any) -> Any:
    raise NotFound("does not exist")


def _raise_trashed(*_args: Any, **_kwargs: Any) -> Any:
    raise DatabricksError("Space 01f has been trashed")


def _raise_permission_denied(*_args: Any, **_kwargs: Any) -> Any:
    raise PermissionDenied("not allowed")


def _return_object(*_args: Any, **_kwargs: Any) -> Any:
    return SimpleNamespace()


def _ws_where_every_lookup(fetch: Any) -> SimpleNamespace:
    """A workspace stub whose every get-style method is *fetch*."""
    service = SimpleNamespace(
        get=fetch,
        get_space=fetch,
        get_index=fetch,
        get_experiment=fetch,
        get_project=fetch,
        get_skill=fetch,
        do=fetch,
    )
    return SimpleNamespace(**dict.fromkeys(_SERVICES, service))


class _Returns:
    def __init__(self, value: Any) -> None:
        self.value = value

    def __call__(self, **_kwargs: Any) -> Any:
        return self.value


class _RecordingVerify:
    def __init__(self) -> None:
        self.calls: list[list[BrickResult]] = []

    def __call__(self, _ws: Any, results: list[BrickResult]) -> list[BrickResult]:
        self.calls.append(results)
        return results


def _ws_returning(attr: str, method: str, value: Any) -> SimpleNamespace:
    return SimpleNamespace(**{attr: SimpleNamespace(**{method: _Returns(value)})})


class _CountdownWs:
    """Genie space that is still visible for *polls_until_gone* lookups."""

    def __init__(self, polls_until_gone: int) -> None:
        self.lookups = 0
        self._remaining = polls_until_gone
        self.genie = SimpleNamespace(get_space=self._get_space)

    def _get_space(self, **_kwargs: Any) -> Any:
        self.lookups += 1
        if self._remaining <= 0:
            raise NotFound("does not exist")
        self._remaining -= 1
        return SimpleNamespace()


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _ref(kind: Any, resource_id: str = "r-1") -> ResourceRef:
    return ResourceRef(kind=kind, resource_id=resource_id)


_KINDS = sorted(gone._GONE_CHECKS)


class TestIsGone:
    @pytest.mark.parametrize("kind", _KINDS)
    def test_not_found_is_gone(self, kind: Any) -> None:
        assert gone.is_gone(_ws_where_every_lookup(_raise_not_found), _ref(kind, "1"))

    @pytest.mark.parametrize("kind", ["genie_space", "vector_index", "job", "app", "uc_schema"])
    def test_resource_still_returned_is_not_gone(self, kind: Any) -> None:
        assert not gone.is_gone(_ws_where_every_lookup(_return_object), _ref(kind, "1"))

    def test_trashed_error_is_gone(self) -> None:
        assert gone.is_gone(_ws_where_every_lookup(_raise_trashed), _ref("genie_space"))

    def test_other_errors_propagate(self) -> None:
        with pytest.raises(PermissionDenied):
            gone.is_gone(_ws_where_every_lookup(_raise_permission_denied), _ref("genie_space"))

    @pytest.mark.parametrize(
        ("kind", "attr", "method", "deleted", "live"),
        [
            (
                "sql_warehouse",
                "warehouses",
                "get",
                SimpleNamespace(state=SimpleNamespace(value="DELETED")),
                SimpleNamespace(state=SimpleNamespace(value="STOPPED")),
            ),
            (
                "dashboard",
                "lakeview",
                "get",
                SimpleNamespace(lifecycle_state=SimpleNamespace(value="TRASHED")),
                SimpleNamespace(lifecycle_state=SimpleNamespace(value="ACTIVE")),
            ),
            (
                "mlflow_experiment",
                "experiments",
                "get_experiment",
                SimpleNamespace(experiment=SimpleNamespace(lifecycle_stage="deleted")),
                SimpleNamespace(experiment=SimpleNamespace(lifecycle_stage="active")),
            ),
            (
                "lakebase_project",
                "postgres",
                "get_project",
                SimpleNamespace(delete_time="2026-10-15T00:00:00Z"),
                SimpleNamespace(delete_time=None),
            ),
        ],
    )
    def test_soft_deleted_is_gone(self, kind: Any, attr: str, method: str, deleted: Any, live: Any) -> None:
        assert gone.is_gone(_ws_returning(attr, method, deleted), _ref(kind))
        assert not gone.is_gone(_ws_returning(attr, method, live), _ref(kind))


class TestWaitUntilGone:
    def test_polls_until_gone(self) -> None:
        ws = _CountdownWs(polls_until_gone=2)
        clock = _FakeClock()

        assert wait_until_gone(ws, [_ref("genie_space")], clock=clock, sleep=clock.sleep) == []
        assert ws.lookups == 3

    def test_returns_refs_still_present_at_timeout(self) -> None:
        ws = _CountdownWs(polls_until_gone=10_000)
        clock = _FakeClock()

        remaining = wait_until_gone(ws, [_ref("genie_space")], clock=clock, sleep=clock.sleep)

        assert remaining == [_ref("genie_space")]
        assert clock.now == DEFAULT_TIMEOUT_SECONDS

    def test_async_kinds_get_longer_timeout(self) -> None:
        ws = _ws_where_every_lookup(_return_object)
        clock = _FakeClock()

        wait_until_gone(ws, [_ref("vector_index")], clock=clock, sleep=clock.sleep)

        assert clock.now > DEFAULT_TIMEOUT_SECONDS


class TestVerifyGone:
    def test_gone_result_unchanged(self) -> None:
        result = BrickResult(name="s", status="ok", deleted=[_ref("genie_space")])

        assert verify_gone(_ws_where_every_lookup(_raise_not_found), [result]) == [result]

    def test_still_present_becomes_destroy_error(self) -> None:
        clock = _FakeClock()
        result = BrickResult(name="s", status="ok", deleted=[_ref("genie_space", "sp-1")])

        [verified] = verify_gone(_ws_where_every_lookup(_return_object), [result], clock=clock, sleep=clock.sleep)

        assert verified.status == "destroy_error"
        assert verified.rc == 1
        assert verified.details["still_exists"] == ["genie_space sp-1"]

    def test_lookup_failure_becomes_destroy_error(self) -> None:
        result = BrickResult(name="s", status="ok", deleted=[_ref("genie_space")])

        [verified] = verify_gone(_ws_where_every_lookup(_raise_permission_denied), [result])

        assert verified.status == "destroy_error"
        assert "could not verify deletion" in verified.details["error"]

    @pytest.mark.parametrize(
        "result",
        [
            BrickResult(name="s", status="skipped", details={"reason": "requires --force"}),
            BrickResult(name="s", status="destroy_error", rc=1, deleted=[_ref("job")]),
            BrickResult(name="s", status="ok"),
        ],
    )
    def test_nothing_to_verify_is_untouched(self, result: BrickResult) -> None:
        assert verify_gone(_ws_where_every_lookup(_raise_permission_denied), [result]) == [result]


class TestDeletedRefs:
    def _ctx(self, tmp_path: Path, *, dry_run: bool) -> DestroyContext:
        return DestroyContext(instance_dir=tmp_path, target="dev", variables={}, dry_run=dry_run)

    def test_success_reports_non_empty_ids(self, tmp_path: Path) -> None:
        refs = deleted_refs(self._ctx(tmp_path, dry_run=False), 0, "uc_function", "c.s.f1", "", None)

        assert refs == [_ref("uc_function", "c.s.f1")]

    @pytest.mark.parametrize(("rc", "dry_run"), [(1, False), (0, True)])
    def test_failure_or_dry_run_reports_nothing(self, tmp_path: Path, rc: int, dry_run: bool) -> None:
        assert deleted_refs(self._ctx(tmp_path, dry_run=dry_run), rc, "job", "1") == []


class _DeletingBrick(BrickDestroyDefaults):
    name = "genie_spaces"
    display_name = "Genie Spaces"
    deploy_order = 30
    state_schema: type[BaseModel] | None = None

    def applies_to(self, ctx: DeployContext) -> bool:  # noqa: ARG002
        return True

    def deploy(self, ctx: DeployContext) -> list[BrickResult]:  # noqa: ARG002
        return []

    def load_state_outputs(self, ctx: DeployContext) -> dict[str, str]:  # noqa: ARG002
        return {}

    def destroy(self, ctx: DestroyContext) -> list[BrickResult]:
        return [BrickResult(name="s", status="ok", deleted=deleted_refs(ctx, 0, "genie_space", "sp-1"))]


class TestOrchestratorWaitsUntilGone:
    @pytest.fixture
    def verified(self, monkeypatch: pytest.MonkeyPatch) -> _RecordingVerify:
        from agent_foundry.deploy import _orchestrator

        recorder = _RecordingVerify()
        monkeypatch.setattr(_orchestrator, "_preload_state", lambda *_a, **_k: None)
        monkeypatch.setattr("agent_foundry.telemetry.emit_lifecycle_telemetry", lambda *_a, **_k: None)
        monkeypatch.setattr(_orchestrator, "verify_gone", recorder)
        return recorder

    def test_destroy_verifies_deleted_resources(self, tmp_path: Path, verified: _RecordingVerify) -> None:
        from agent_foundry.deploy._orchestrator import destroy_instance

        destroy_instance(tmp_path, "dev", variables={}, bricks=[_DeletingBrick()], ws=SimpleNamespace())

        assert [r.deleted for r in verified.calls[0]] == [[_ref("genie_space", "sp-1")]]

    @pytest.mark.parametrize(("dry_run", "offline"), [(True, False), (False, True)])
    def test_dry_run_and_offline_do_not_wait(
        self, tmp_path: Path, verified: _RecordingVerify, dry_run: bool, offline: bool
    ) -> None:
        from agent_foundry.deploy._orchestrator import destroy_instance

        destroy_instance(
            tmp_path,
            "dev",
            variables={},
            bricks=[_DeletingBrick()],
            ws=SimpleNamespace(),
            dry_run=dry_run,
            offline=offline,
        )

        assert verified.calls == []
