"""Tests for :mod:`payloads.agent.runtime.brick_mcp_servers`."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from agent_foundry._instance_layout import resolve_payload_subdir
from payloads.agent.runtime.brick_mcp_servers import (
    _BrickSpecLoad,
    _clear_brick_specs_cache,
    _load_brick_spec_status,
    _load_brick_specs,
    build_mcp_servers,
    build_mcp_servers_with_status,
)
from payloads.agent.runtime.mcp_availability import UnavailableServer


class TestBuildMcpServersSpecCache:
    """Process-level memoization for the brick-state spec list.

    The deploy-time spec list is read from the workspace state files
    (~100ms per server, sequential).  Production trace showed this
    costing ~0.4s on every request.  We cache the parsed specs once
    per process and rebuild the wrapper objects (with per-request
    OAuth provider) on each call -- so OBO semantics are preserved.

    These tests pin three invariants:

    1. ``_load_brick_specs`` runs at most once per
       ``(instance_dir, target)`` key.
    2. The wrapper construction (``_mcp_servers_from_specs``) runs on
       every call -- it carries the per-request ``auth_ws``.
    3. The test escape hatch actually clears the cache.
    """

    def setup_method(self) -> None:
        _clear_brick_specs_cache()

    def teardown_method(self) -> None:
        _clear_brick_specs_cache()

    @patch("payloads.agent.runtime.brick_mcp_servers._mcp_servers_from_specs")
    @patch("payloads.agent.runtime.brick_mcp_servers._load_brick_spec_status")
    @patch("payloads.agent.runtime.request_context.get_user_workspace_client", return_value=None)
    @patch("payloads.agent.runtime.brick_mcp_servers.resolve_instance_dir")
    def test_load_runs_once_specs_construction_runs_per_call(
        self,
        mock_resolve: MagicMock,
        _mock_user_ws: MagicMock,
        mock_load: MagicMock,
        mock_construct: MagicMock,
        tmp_path: Path,
    ) -> None:
        mock_resolve.return_value = tmp_path
        mock_load.return_value = _BrickSpecLoad(specs=(cast(Any, MagicMock()),))
        mock_construct.return_value = []
        sp_ws = MagicMock()

        for _ in range(5):
            build_mcp_servers({}, sp_ws)

        assert mock_load.call_count == 1, (
            f"_load_brick_spec_status should run once per process per "
            f"(instance_dir, target); was called {mock_load.call_count} times"
        )
        assert mock_construct.call_count == 5, (
            "Wrapper construction must run on every call so the per-request "
            f"auth_ws binds correctly; was called {mock_construct.call_count} "
            "times"
        )

    @patch("payloads.agent.runtime.brick_mcp_servers._mcp_servers_from_specs", return_value=[])
    @patch("payloads.agent.runtime.brick_mcp_servers._load_brick_spec_status", return_value=_BrickSpecLoad())
    @patch("payloads.agent.runtime.request_context.get_user_workspace_client", return_value=None)
    @patch("payloads.agent.runtime.brick_mcp_servers.resolve_instance_dir")
    def test_clear_brick_specs_cache_resets_state(
        self,
        mock_resolve: MagicMock,
        _mock_user_ws: MagicMock,
        mock_load: MagicMock,
        _mock_construct: MagicMock,
        tmp_path: Path,
    ) -> None:
        mock_resolve.return_value = tmp_path
        sp_ws = MagicMock()

        build_mcp_servers({}, sp_ws)
        build_mcp_servers({}, sp_ws)
        assert mock_load.call_count == 1

        _clear_brick_specs_cache()
        build_mcp_servers({}, sp_ws)
        assert mock_load.call_count == 2


class TestLoadBrickSpecsDedup:
    """Regression: two brick subdirs sharing an MCP URL both register.

    Several managed MCP types have schema- or singleton-scoped URLs:

    * ``managed_vector_search`` -> ``{host}/api/2.0/mcp/vector-search/{catalog}/{schema}``
    * ``managed_functions``     -> ``{host}/api/2.0/mcp/functions/{catalog}/{schema}``
    * ``managed_sql``           -> ``{host}/api/2.0/mcp/sql``

    Two brick subdirs can legitimately resolve to the same URL while
    exposing disjoint tool sets (e.g. ``*__docs_index`` vs
    ``*__products_index`` against the same ``{catalog}/{schema}``).
    Previously the loader keyed dedup on URL and silently dropped the
    second subdir.  Dedup now keys on ``sub.name``, which is the only
    truly unique identifier for a server registration.
    """

    @staticmethod
    def _make_state(allow: list[str]) -> dict[str, Any]:
        return {
            "url": "https://ws.example.com/api/2.0/mcp/vector-search/cat/sch",
            "name_prefix": "",
            "tools": {"allow": allow, "default_meta": {}},
            "prompts": {},
            "resources": {},
        }

    def test_two_subdirs_sharing_url_both_register(self, tmp_path: Path) -> None:
        mcp_dir = resolve_payload_subdir(tmp_path, "mcp_servers")
        (mcp_dir / "docs_search").mkdir(parents=True)
        (mcp_dir / "products_search").mkdir()

        state_by_sub = {
            "docs_search": self._make_state(["*__docs_index"]),
            "products_search": self._make_state(["*__products_index"]),
        }

        def fake_load_state(
            _ws: Any,
            _instance: str,
            _kind: str,
            sub_name: str,
            _target: str,
        ) -> dict[str, Any]:
            return state_by_sub[sub_name]

        with patch("agent_foundry.state.load_state", side_effect=fake_load_state):
            specs = _load_brick_specs(tmp_path, "dev", MagicMock())

        assert [s.sub_name for s in specs] == ["docs_search", "products_search"], (
            "both brick subdirs must register even when their URLs are identical; "
            "regression for URL-keyed dedup silently dropping the second subdir"
        )
        assert specs[0].url == specs[1].url

    def test_warns_and_drops_only_on_duplicate_subdir_name(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # The filesystem prevents two subdirs with the same name, so this
        # test pins the only remaining dedup trigger (defensive guard):
        # identical URLs across distinct subdirs must NOT warn.
        mcp_dir = resolve_payload_subdir(tmp_path, "mcp_servers")
        (mcp_dir / "vs_a").mkdir(parents=True)
        (mcp_dir / "vs_b").mkdir()

        def fake_load_state(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return self._make_state(["*__x"])

        with caplog.at_level(logging.WARNING, logger="payloads.agent.runtime.core"):
            with patch("agent_foundry.state.load_state", side_effect=fake_load_state):
                specs = _load_brick_specs(tmp_path, "dev", MagicMock())

        assert len(specs) == 2
        duplicate_warnings = [r for r in caplog.records if "duplicate" in r.getMessage()]
        assert duplicate_warnings == [], (
            "shared URL across distinct subdirs must not trigger the duplicate "
            f"warning; got: {[r.getMessage() for r in duplicate_warnings]}"
        )


class TestBrickSpecStatus:
    """Broken brick state is reported as unavailable; an undeployed brick is not."""

    STATE_BY_SUB: dict[str, dict[str, Any]] = {
        "good": {"url": "https://h/api/2.0/mcp/genie/x", "tools": {"allow": ["*"]}},
        "no_allow": {"url": "https://h/api/2.0/mcp/genie/y", "tools": {"allow": []}},
        "undeployed": {},
    }

    def setup_method(self) -> None:
        _clear_brick_specs_cache()

    def teardown_method(self) -> None:
        _clear_brick_specs_cache()

    def _fake_load_state(self, _ws: Any, _instance: str, _kind: str, sub_name: str, _target: str) -> dict[str, Any]:
        return self.STATE_BY_SUB[sub_name]

    def _make_dirs(self, root: Path) -> None:
        mcp_dir = resolve_payload_subdir(root, "mcp_servers")
        for sub in self.STATE_BY_SUB:
            (mcp_dir / sub).mkdir(parents=True)

    def test_invalid_state_skipped_and_reported(self, tmp_path: Path) -> None:
        self._make_dirs(tmp_path)

        with patch("agent_foundry.state.load_state", side_effect=self._fake_load_state):
            load = _load_brick_spec_status(tmp_path, "dev", MagicMock())

        assert [spec.sub_name for spec in load.specs] == ["good"]
        assert load.skipped == (UnavailableServer("no_allow", "invalid_state"),)

    @patch("payloads.agent.runtime.request_context.get_user_workspace_client", return_value=None)
    @patch("payloads.agent.runtime.brick_mcp_servers.resolve_instance_dir")
    def test_build_reports_cached_skips(
        self,
        mock_resolve: MagicMock,
        _user: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("DEPLOY_TARGET", "dev")
        self._make_dirs(tmp_path)
        mock_resolve.return_value = tmp_path

        with patch("agent_foundry.state.load_state", side_effect=self._fake_load_state) as load_state:
            first = build_mcp_servers_with_status({}, MagicMock())
            second = build_mcp_servers_with_status({}, MagicMock())

        assert load_state.call_count == len(self.STATE_BY_SUB)
        assert [server.name for server in second.servers] == ["mcp-brick-good"]
        assert first.unavailable == second.unavailable == (UnavailableServer("no_allow", "invalid_state"),)
