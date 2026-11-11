"""Tests for Vector Search deploy script tagging (offline -- no workspace needed)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_foundry import state as shared_state
from agent_foundry.bricks import DestroyContext
from agent_foundry.destroy_base import NOTHING_TO_DESTROY
from agent_foundry.testing.fakes import FakeWorkspaceClient
from bricks.vector_search.brick import VectorIndexesBrick

BRICK_ROOT = Path(__file__).resolve().parents[2]


class TestDestroyIndex:
    """Test _destroy_index idempotency."""

    def _client_with_delete(self, side_effect: Exception | None) -> Any:
        class _IndexClient:
            def delete_index(self, index_name: str) -> None:  # noqa: ARG002
                if side_effect is not None:
                    raise side_effect

        class _Client:
            vector_search_indexes = _IndexClient()

        return _Client()

    def test_already_deleted_is_success(self, capsys: pytest.CaptureFixture[str]) -> None:
        from bricks.vector_search.scripts.deploy_index import _destroy_index

        client = self._client_with_delete(
            RuntimeError("RESOURCE_DOES_NOT_EXIST: Unity Catalog entity foo.bar.baz does not exist.")
        )
        _destroy_index(client, "foo.bar.baz", dry_run=False)
        assert "already gone" in capsys.readouterr().out

    def test_other_error_is_reraised(self) -> None:
        from bricks.vector_search.scripts.deploy_index import _destroy_index

        client = self._client_with_delete(RuntimeError("PERMISSION_DENIED"))
        with pytest.raises(RuntimeError, match="PERMISSION_DENIED"):
            _destroy_index(client, "foo.bar.baz", dry_run=False)


class _RecordingIndexClient:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    def delete_index(self, index_name: str) -> None:
        self.deleted.append(index_name)


class _RecordingClient(FakeWorkspaceClient):
    def __init__(self) -> None:
        super().__init__()
        self.vector_search_indexes = _RecordingIndexClient()


def _shared_state(client: _RecordingClient) -> dict[str, Any]:
    """Read state the way ``VectorIndexesBrick`` does (workspace, brick type ``vector_indexes``)."""
    return shared_state.load_state(client, "demo", "vector_indexes", "vs_smoke", "dev")


class TestSyncIndex:
    """``_sync_index`` skips a sync the pipeline cannot take yet."""

    def _client_with_sync(self, side_effect: Exception) -> Any:
        class _IndexClient:
            def sync_index(self, index_name: str) -> None:  # noqa: ARG002
                raise side_effect

        class _Client:
            vector_search_indexes = _IndexClient()

        return _Client()

    def test_pending_pipeline_update_is_skipped(self, capsys: pytest.CaptureFixture[str]) -> None:
        from databricks.sdk.errors import BadRequest

        from bricks.vector_search.scripts.deploy_index import _sync_index

        client = self._client_with_sync(
            BadRequest(
                "Index is not ready to sync yet. Pipeline is in state CREATED and needs to be in "
                "one of the following states to sync: COMPLETED, FAILED, CANCELED."
            )
        )
        _sync_index(client, "cat.sch.idx", dry_run=False)
        assert "already pending" in capsys.readouterr().out

    def test_other_bad_request_is_reraised(self) -> None:
        from databricks.sdk.errors import BadRequest

        from bricks.vector_search.scripts.deploy_index import _sync_index

        client = self._client_with_sync(BadRequest("Index type DIRECT_ACCESS does not support sync"))
        with pytest.raises(BadRequest, match="does not support sync"):
            _sync_index(client, "cat.sch.idx", dry_run=False)


class TestSharedState:
    """deploy_index persists to the shared workspace state, not a local file."""

    def test_create_saves_index_to_shared_state(
        self,
        vs_instance_dir: Path,
        vs_variables: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from bricks.vector_search.scripts import deploy_index as mod

        monkeypatch.setattr(mod, "_ensure_endpoint", lambda *a, **kw: None)
        monkeypatch.setattr(mod, "_tag_endpoint", lambda *a, **kw: None)
        monkeypatch.setattr(mod, "_tag_index", lambda *a, **kw: None)
        created = {"index_name": "cat.sch.idx", "endpoint_name": "ep", "index_type": "DELTA_SYNC"}
        monkeypatch.setattr(mod, "_create_index", lambda *a, **kw: dict(created))
        client = _RecordingClient()

        rc = mod.deploy(vs_instance_dir, "dev", vs_variables, ws=client)

        assert rc == 0
        assert {k: _shared_state(client)[k] for k in created} == created
        assert not (vs_instance_dir / "_state").exists()

    def test_destroy_uses_state_without_create_only_vars(self, vs_instance_dir: Path) -> None:
        from bricks.vector_search.scripts.deploy_index import deploy

        client = _RecordingClient()
        shared_state.save_state(client, "demo", "vector_indexes", "vs_smoke", "dev", {"index_name": "cat.sch.idx"})

        rc = deploy(vs_instance_dir, "dev", {"catalog": "cat"}, action="destroy", ws=client)

        assert rc == 0
        assert client.vector_search_indexes.deleted == ["cat.sch.idx"]
        assert _shared_state(client) == {}

    def test_destroy_without_state_still_validates_config(self, vs_instance_dir: Path) -> None:
        from bricks.vector_search.scripts.deploy_index import deploy

        client = _RecordingClient()

        rc = deploy(vs_instance_dir, "dev", {"catalog": "cat"}, action="destroy", ws=client)

        assert rc == 1
        assert client.vector_search_indexes.deleted == []

    def test_brick_skips_scaffolded_config_without_state(self, vs_instance_dir: Path) -> None:
        """No index in the shared state means nothing to destroy, even with an unresolved config (#542)."""
        client = _RecordingClient()
        ctx = DestroyContext(instance_dir=vs_instance_dir.parents[1], target="dev", ws=client, force=True)

        results = VectorIndexesBrick().destroy(ctx)

        assert [(r.status, r.details) for r in results] == [("skipped", {"reason": NOTHING_TO_DESTROY})]
        assert client.vector_search_indexes.deleted == []


class TestTagEndpoint:
    """Test _tag_endpoint helper."""

    def test_dry_run_prints_tags(self, capsys: pytest.CaptureFixture[str]) -> None:
        from bricks.vector_search.scripts.deploy_index import _tag_endpoint

        _tag_endpoint(None, "my-endpoint", {"ai_solution_managed_by": "test-app"}, dry_run=True)
        output = capsys.readouterr().out
        assert "Would tag endpoint" in output
        assert "test-app" in output

    def test_empty_tags_is_noop(self) -> None:
        from bricks.vector_search.scripts.deploy_index import _tag_endpoint

        _tag_endpoint(None, "my-endpoint", {}, dry_run=False)

    def test_empty_tags_dry_run_no_output(self, capsys: pytest.CaptureFixture[str]) -> None:
        from bricks.vector_search.scripts.deploy_index import _tag_endpoint

        _tag_endpoint(None, "my-endpoint", {}, dry_run=True)
        output = capsys.readouterr().out
        assert "tag" not in output.lower()

    def test_unsupported_tags_demoted_to_info(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Vector Search endpoints do not accept custom tags (product
        limitation).  The failure must not surface as a scary WARNING
        because it is expected on every deploy against real VS."""
        from unittest.mock import MagicMock

        from bricks.vector_search.scripts.deploy_index import _tag_endpoint

        client = MagicMock()
        client.vector_search_endpoints.update_endpoint_custom_tags.side_effect = Exception(
            "Custom tags are not supported in Vector Search"
        )
        _tag_endpoint(client, "my-endpoint", {"k": "v"}, dry_run=False)
        output = capsys.readouterr().out
        assert "WARNING" not in output
        assert "product limitation" in output.lower()

    def test_unexpected_error_still_warns(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Any failure mode that isn't the documented product
        limitation must still be visible as a WARNING."""
        from unittest.mock import MagicMock

        from bricks.vector_search.scripts.deploy_index import _tag_endpoint

        client = MagicMock()
        client.vector_search_endpoints.update_endpoint_custom_tags.side_effect = Exception("network unreachable")
        _tag_endpoint(client, "my-endpoint", {"k": "v"}, dry_run=False)
        output = capsys.readouterr().out
        assert "WARNING" in output
        assert "network unreachable" in output


class TestEnsureEndpointOptions:
    """``_ensure_endpoint`` forwards the optional endpoint knobs to the SDK
    only when set, and passes endpoint_type through as the SDK enum."""

    @staticmethod
    def _missing_endpoint_client() -> Any:
        """A client whose get_endpoint raises (endpoint absent), capturing
        the kwargs passed to create_endpoint_and_wait."""
        from unittest.mock import MagicMock

        client = MagicMock()
        client.vector_search_endpoints.get_endpoint.side_effect = Exception("RESOURCE_DOES_NOT_EXIST")
        return client

    def test_forwards_options_when_set(self) -> None:
        from databricks.sdk.service.vectorsearch import EndpointType

        from bricks.vector_search.scripts.deploy_index import _ensure_endpoint

        client = self._missing_endpoint_client()
        existed = _ensure_endpoint(
            client,
            "ep",
            "STORAGE_OPTIMIZED",
            dry_run=False,
            budget_policy_id="bp-1",
            target_qps=500,
            usage_policy_id="up-1",
        )
        assert existed is False
        _, kwargs = client.vector_search_endpoints.create_endpoint_and_wait.call_args
        assert kwargs["endpoint_type"] == EndpointType.STORAGE_OPTIMIZED
        assert kwargs["budget_policy_id"] == "bp-1"
        assert kwargs["target_qps"] == 500
        assert kwargs["usage_policy_id"] == "up-1"

    def test_omits_options_when_unset(self) -> None:
        from databricks.sdk.service.vectorsearch import EndpointType

        from bricks.vector_search.scripts.deploy_index import _ensure_endpoint

        client = self._missing_endpoint_client()
        _ensure_endpoint(client, "ep", "STANDARD", dry_run=False)
        _, kwargs = client.vector_search_endpoints.create_endpoint_and_wait.call_args
        assert kwargs["endpoint_type"] == EndpointType.STANDARD
        assert "budget_policy_id" not in kwargs
        assert "target_qps" not in kwargs
        assert "usage_policy_id" not in kwargs


class TestSyncGateOnReadiness:
    """Regression: an existing DELTA_SYNC index that is still provisioning
    (``ready=False``) must NOT trigger a manual ``sync_index`` call.  The
    Databricks API rejects a manual sync on a non-ready index with 400
    BadRequest, which crashes the deploy on first-run workspaces.  A fresh
    DELTA_SYNC index auto-syncs from its source table once it first reaches
    READY, so skipping the explicit sync is both correct and required.
    """

    def _seed_fixture(
        self,
        vs_instance_dir: Path,
        vs_variables: dict[str, str],
        *,
        ready: bool,
    ) -> tuple[Path, dict[str, str], list[str], FakeWorkspaceClient]:
        """Seed workspace state with an existing index and return a
        ``sync_calls`` list the caller can assert on."""
        ws = FakeWorkspaceClient()
        app, sub = vs_instance_dir.parent.parent.name, vs_instance_dir.name
        shared_state.save_state(ws, app, "vector_indexes", sub, "dev", {"index_name": "main.test.docs_index"})
        return vs_instance_dir, vs_variables, [], ws

    def _install_mocks(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sync_calls: list[str],
        *,
        ready: bool,
    ) -> None:
        from bricks.vector_search.scripts import deploy_index as mod

        monkeypatch.setattr(mod, "_get_client", lambda variables, **_k: object())
        monkeypatch.setattr(mod, "_ensure_endpoint", lambda *a, **kw: None)
        monkeypatch.setattr(mod, "_tag_endpoint", lambda *a, **kw: None)
        monkeypatch.setattr(mod, "_tag_index", lambda *a, **kw: None)
        monkeypatch.setattr(
            mod,
            "_get_index_status",
            lambda client, name: {
                "ready": ready,
                "indexed_row_count": 100 if ready else 0,
                "message": "READY" if ready else "PROVISIONING",
            },
        )

        def _record_sync(client: Any, index_name: str, dry_run: bool) -> None:  # noqa: ARG001
            sync_calls.append(index_name)

        monkeypatch.setattr(mod, "_sync_index", _record_sync)

    def test_skips_sync_when_index_not_ready(
        self,
        vs_instance_dir: Path,
        vs_variables: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from bricks.vector_search.scripts.deploy_index import deploy

        instance_dir, variables, sync_calls, ws = self._seed_fixture(
            vs_instance_dir,
            vs_variables,
            ready=False,
        )
        self._install_mocks(monkeypatch, sync_calls, ready=False)

        rc = deploy(instance_dir, "dev", variables, dry_run=False, ws=ws)

        assert rc == 0
        assert sync_calls == [], f"_sync_index must not be called for a non-ready index; got calls: {sync_calls}"
        assert "still provisioning" in capsys.readouterr().out

    def test_triggers_sync_when_index_ready(
        self,
        vs_instance_dir: Path,
        vs_variables: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from bricks.vector_search.scripts.deploy_index import deploy

        instance_dir, variables, sync_calls, ws = self._seed_fixture(
            vs_instance_dir,
            vs_variables,
            ready=True,
        )
        self._install_mocks(monkeypatch, sync_calls, ready=True)

        rc = deploy(instance_dir, "dev", variables, dry_run=False, ws=ws)

        assert rc == 0
        assert sync_calls == ["main.test.docs_index"]
