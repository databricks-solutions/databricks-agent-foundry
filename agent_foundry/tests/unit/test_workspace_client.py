"""Tests for the shared WorkspaceClient factory."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from agent_foundry.telemetry import product_kwargs
from agent_foundry.workspace_client import (
    SKIP_DEFAULT_WORKSPACE_CLIENT_ENV,
    export_mlflow_auth_env,
    get_workspace_client,
    prefer_workspace_client,
)

MODULE = "databricks.sdk.WorkspaceClient"


@pytest.fixture(autouse=True)
def _allow_workspace_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SKIP_DEFAULT_WORKSPACE_CLIENT_ENV, raising=False)


@patch(MODULE)
def test_no_profile_uses_default(mock_ws_cls: MagicMock):
    get_workspace_client()
    mock_ws_cls.assert_called_once_with(**product_kwargs())


@patch(MODULE)
def test_profile_from_variables(mock_ws_cls: MagicMock):
    get_workspace_client({"profile": "my-profile", "host": "https://x"})
    mock_ws_cls.assert_called_once_with(profile="my-profile", **product_kwargs())


@patch(MODULE)
def test_explicit_profile_overrides_variables(mock_ws_cls: MagicMock):
    get_workspace_client({"profile": "from-vars"}, profile="explicit")
    mock_ws_cls.assert_called_once_with(profile="explicit", **product_kwargs())


@patch(MODULE)
def test_empty_profile_falls_back_to_host(mock_ws_cls: MagicMock):
    """``init`` writes host with an empty profile; do not use [DEFAULT]."""
    get_workspace_client({"profile": "", "host": "https://x"})
    mock_ws_cls.assert_called_once_with(host="https://x", **product_kwargs())


@patch(MODULE)
def test_host_only_uses_host(mock_ws_cls: MagicMock):
    get_workspace_client({"host": "https://adb.example.azuredatabricks.net"})
    mock_ws_cls.assert_called_once_with(
        host="https://adb.example.azuredatabricks.net",
        **product_kwargs(),
    )


@patch(MODULE)
def test_empty_host_without_profile_uses_default(mock_ws_cls: MagicMock):
    get_workspace_client({"profile": "", "host": ""})
    mock_ws_cls.assert_called_once_with(**product_kwargs())


@patch(MODULE)
def test_none_variables_uses_default(mock_ws_cls: MagicMock):
    get_workspace_client(None)
    mock_ws_cls.assert_called_once_with(**product_kwargs())


@patch(MODULE)
def test_opt_out_omits_product(mock_ws_cls: MagicMock, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGENT_FOUNDRY_TELEMETRY_DISABLED", "true")
    get_workspace_client()
    mock_ws_cls.assert_called_once_with()


def test_skip_env_refuses_to_construct_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SKIP_DEFAULT_WORKSPACE_CLIENT_ENV, "true")
    with pytest.raises(RuntimeError, match=SKIP_DEFAULT_WORKSPACE_CLIENT_ENV):
        get_workspace_client({"profile": "dry-run"})


def test_prefer_returns_injected_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SKIP_DEFAULT_WORKSPACE_CLIENT_ENV, "true")
    sentinel = object()
    assert prefer_workspace_client({}, ws=sentinel) is sentinel


def test_prefer_returns_none_when_skip_env_and_no_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SKIP_DEFAULT_WORKSPACE_CLIENT_ENV, "true")
    assert prefer_workspace_client({"profile": "dry-run"}) is None


class TestExportMlflowAuthEnv:
    """MLflow builds its own WorkspaceClient from ambient auth, so the
    resolved profile/host must reach it via the environment (#421).
    """

    def test_sets_profile_and_host_when_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)
        monkeypatch.delenv("DATABRICKS_HOST", raising=False)
        export_mlflow_auth_env({"profile": "prof-x", "host": "https://h.example"})
        assert os.environ["DATABRICKS_CONFIG_PROFILE"] == "prof-x"
        assert os.environ["DATABRICKS_HOST"] == "https://h.example"

    def test_noop_when_variables_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)
        monkeypatch.delenv("DATABRICKS_HOST", raising=False)
        export_mlflow_auth_env({})
        assert "DATABRICKS_CONFIG_PROFILE" not in os.environ
        assert "DATABRICKS_HOST" not in os.environ

    def test_does_not_override_existing_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``setdefault`` semantics: an explicit caller env wins."""
        monkeypatch.setenv("DATABRICKS_CONFIG_PROFILE", "already-set")
        export_mlflow_auth_env({"profile": "prof-x"})
        assert os.environ["DATABRICKS_CONFIG_PROFILE"] == "already-set"
