"""Tests for :mod:`payloads.agent.runtime.runtime_config`."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_foundry.workspace_client import SKIP_DEFAULT_WORKSPACE_CLIENT_ENV
from payloads.agent.runtime.request_context import get_workspace_client
from payloads.agent.runtime.runtime_config import (
    _add_computed_variables,
    _clear_agent_config_cache,
    _get_runtime_workspace_client,
    _instance_identity_variables,
    _preload_runtime_state,
    load_agent_config_and_vars,
)


class TestRuntimeWorkspaceClient:
    """Issue #676: state preload reuses the App's client instead of building a second one."""

    def test_reuses_the_app_client(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(SKIP_DEFAULT_WORKSPACE_CLIENT_ENV, raising=False)
        monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)
        with patch(
            "payloads.agent.runtime.request_context.WorkspaceClient", side_effect=lambda **_: MagicMock()
        ) as ctor:
            assert _get_runtime_workspace_client() is get_workspace_client()

        ctor.assert_called_once()

    def test_builds_nothing_under_the_skip_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(SKIP_DEFAULT_WORKSPACE_CLIENT_ENV, "true")
        with patch("payloads.agent.runtime.request_context.WorkspaceClient") as ctor:
            assert _get_runtime_workspace_client() is None

        ctor.assert_not_called()

    def test_none_when_construction_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(SKIP_DEFAULT_WORKSPACE_CLIENT_ENV, raising=False)
        monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)
        with patch("payloads.agent.runtime.request_context.WorkspaceClient", side_effect=ValueError("no credentials")):
            assert _get_runtime_workspace_client() is None


class TestPreloadRuntimeState:
    """Regression guard for issue #50: the deployed agent must read state
    from the workspace, not from a stale local cache."""

    def test_passes_workspace_client_to_load_state_outputs(self, tmp_path: Path) -> None:
        from agent_foundry.public import StateOutputs

        ws_sentinel = MagicMock(name="WorkspaceClient")
        empty = StateOutputs(variables={}, bricks_loaded=[])
        with (
            patch("payloads.agent.runtime.runtime_config._get_runtime_workspace_client", return_value=ws_sentinel),
            patch("agent_foundry.config.load_state_outputs", return_value=empty) as mock_load,
        ):
            _preload_runtime_state(tmp_path, "dev", {})

        mock_load.assert_called_once()
        assert mock_load.call_args.kwargs.get("ws") is ws_sentinel, (
            "runtime must pass a WorkspaceClient to load_state_outputs"
        )

    def test_falls_back_to_none_when_ws_construction_fails(self, tmp_path: Path) -> None:
        from agent_foundry.public import StateOutputs

        empty = StateOutputs(variables={}, bricks_loaded=[])
        with (
            patch("payloads.agent.runtime.runtime_config._get_runtime_workspace_client", return_value=None),
            patch("agent_foundry.config.load_state_outputs", return_value=empty) as mock_load,
        ):
            _preload_runtime_state(tmp_path, "dev", {})

        assert mock_load.call_args.kwargs.get("ws") is None, "fallback path must pass ws=None for local-only resolution"


class TestLoadAgentConfigAndVarsCache:
    """Process-level memoization for ``load_agent_config_and_vars``.

    Cold-start observation: the underlying loader takes ~2.6s due to
    workspace state reads.  All inputs are deploy-time state, so the
    result is cached for the lifetime of the process.  These tests
    pin three invariants:

    1. The expensive loader runs exactly once per process.
    2. Caller mutations to either returned dict do not pollute the
       cache.
    3. The test-only ``_clear_agent_config_cache`` actually clears
       the cache.
    """

    def setup_method(self) -> None:
        _clear_agent_config_cache()

    def teardown_method(self) -> None:
        _clear_agent_config_cache()

    @patch("payloads.agent.runtime.runtime_config._load_agent_config_and_vars_uncached")
    def test_loader_invoked_once_across_calls(self, mock_inner: MagicMock) -> None:
        mock_inner.return_value = (
            {"LLM_ENDPOINT_NAME": "m", "SYSTEM_PROMPT": "p"},
            {"deployer": "alice@example.com"},
        )
        ws = MagicMock()

        for _ in range(5):
            load_agent_config_and_vars(ws)

        assert mock_inner.call_count == 1, f"loader should run once, was called {mock_inner.call_count} times"

    @patch("payloads.agent.runtime.runtime_config._load_agent_config_and_vars_uncached")
    def test_caller_mutation_does_not_pollute_cache(self, mock_inner: MagicMock) -> None:
        """The handlers inject ``_resolved_username`` and other
        per-request fields into ``config``.  The cached payload must
        survive that."""
        mock_inner.return_value = (
            {"LLM_ENDPOINT_NAME": "m", "SYSTEM_PROMPT": "base"},
            {"deployer": "alice@example.com"},
        )
        ws = MagicMock()

        config_a, vars_a = load_agent_config_and_vars(ws)
        config_a["_resolved_username"] = "user-a"
        vars_a["per_request_marker"] = "polluted"

        config_b, vars_b = load_agent_config_and_vars(ws)
        assert "_resolved_username" not in config_b
        assert "per_request_marker" not in vars_b

    @patch("payloads.agent.runtime.runtime_config._load_agent_config_and_vars_uncached")
    def test_clear_cache_resets_state(self, mock_inner: MagicMock) -> None:
        mock_inner.return_value = ({"LLM_ENDPOINT_NAME": "m"}, {})
        ws = MagicMock()

        load_agent_config_and_vars(ws)
        load_agent_config_and_vars(ws)
        assert mock_inner.call_count == 1

        _clear_agent_config_cache()
        load_agent_config_and_vars(ws)
        assert mock_inner.call_count == 2


class TestComputedVariables:
    """``agent_schema`` has to resolve in both deployment shapes."""

    def test_derived_from_catalog_and_prefix(self) -> None:
        variables = {"catalog": "cat", "schema_prefix": "pa"}
        _add_computed_variables(variables)

        assert variables["agent_schema"] == "cat.pa_agent"

    def test_falls_back_to_the_deploy_time_env_var(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A deployed app has no ``catalog``.

        It lives in the repo-root foundry.toml, which is not staged into
        the App, so without this fallback anything needing the agent
        schema silently no-ops in production while passing locally.
        """
        monkeypatch.setenv("AGENT_SCHEMA", "cat.pa_agent")
        variables: dict[str, str] = {}
        _add_computed_variables(variables)

        assert variables["agent_schema"] == "cat.pa_agent"

    def test_authored_value_is_never_overwritten(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("AGENT_SCHEMA", "from.env")
        variables = {"agent_schema": "explicit.schema"}
        _add_computed_variables(variables)

        assert variables["agent_schema"] == "explicit.schema"

    def test_absent_everywhere_leaves_it_unset(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("AGENT_SCHEMA", raising=False)
        variables: dict[str, str] = {}
        _add_computed_variables(variables)

        assert "agent_schema" not in variables

    def test_catalog_and_prefix_fall_back_to_the_deploy_time_env_vars(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("AGENT_CATALOG", "cat")
        monkeypatch.setenv("AGENT_SCHEMA_PREFIX", "pa")
        variables: dict[str, str] = {}
        _add_computed_variables(variables)

        assert variables["catalog"] == "cat"
        assert variables["schema_prefix"] == "pa"
        assert variables["agent_schema"] == "cat.pa_agent"

    def test_authored_catalog_is_never_overwritten(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("AGENT_CATALOG", "from_env")
        variables = {"catalog": "explicit"}
        _add_computed_variables(variables)

        assert variables["catalog"] == "explicit"

    def test_deployed_example_resolves_its_gateway_fqns(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Deployed shape: no foundry.toml variables, only app.yaml env (canary #570)."""
        from payloads.agent.config import load_agent_spec

        monkeypatch.setenv("AGENT_CATALOG", "cat")
        monkeypatch.setenv("AGENT_SCHEMA_PREFIX", "pa")
        variables: dict[str, str] = {}
        _add_computed_variables(variables)
        example = Path(__file__).parents[5] / "examples" / "unity_gateway_assistant"

        spec = load_agent_spec(example, variables=variables)

        gateway = spec.runtime.tools.skills.effective_gateway()
        assert gateway is not None
        assert [p.fqn for p in gateway.pinned] == ["cat.pa_skills.sql-runner"]


class TestInstanceIdentityVariables:
    """The runtime reads identity from the staged per-instance foundry.toml."""

    def test_reads_authored_identity(self, tmp_path: Path) -> None:
        (tmp_path / "foundry.toml").write_text(
            'name = "pa"\nschema_prefix = "pa_prefix"\nvs_endpoint_name = "vs"\n', encoding="utf-8"
        )

        assert _instance_identity_variables(tmp_path) == {
            "app_name": "pa",
            "schema_prefix": "pa_prefix",
            "vs_endpoint_name": "vs",
        }

    def test_omits_empty_fields(self, tmp_path: Path) -> None:
        (tmp_path / "foundry.toml").write_text('name = "pa"\nschema_prefix = "pa"\n', encoding="utf-8")

        assert "vs_endpoint_name" not in _instance_identity_variables(tmp_path)

    def test_missing_file_yields_nothing(self, tmp_path: Path) -> None:
        assert _instance_identity_variables(tmp_path) == {}
