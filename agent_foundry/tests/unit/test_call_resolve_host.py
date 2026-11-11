"""Tests for ``agent_foundry.cli.call`` host/profile resolution.

The instance is loaded from ``foundry.toml``, so the ``apps.get`` lookup
authenticates with the target's ``profile`` rather than ambient default
auth, which 401s when ``~/.databrickscfg`` holds multiple profiles that
share a host (#374).
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

pytest.importorskip("databricks.sdk", reason="databricks-sdk not installed")

from agent_foundry.cli import call as call_mod  # noqa: E402  # importorskip gate
from agent_foundry.telemetry import product_kwargs  # noqa: E402  # importorskip gate
from agent_foundry.workspace_client import SKIP_DEFAULT_WORKSPACE_CLIENT_ENV  # noqa: E402


@pytest.fixture(autouse=True)
def _allow_workspace_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SKIP_DEFAULT_WORKSPACE_CLIENT_ENV, raising=False)


def _write_repo(root: Path, profile: str) -> None:
    (root / "foundry.toml").write_text(
        "schema_version = 2\n\n"
        "[targets.dev]\n"
        'host = "https://ws.cloud.databricks.com"\n'
        'catalog = "cat"\n'
        f'profile = "{profile}"\n\n'
        '[[instances]]\npath = "instances/demo"\n'
    )
    instance_dir = root / "instances" / "demo"
    instance_dir.mkdir(parents=True)
    (instance_dir / "foundry.toml").write_text('name = "demo"\nschema_prefix = "demo"\n')


def _resolve(root: Path, **kwargs: str) -> tuple[tuple[str, str | None], mock.MagicMock]:
    fake_ws = mock.MagicMock()
    fake_ws.apps.get.return_value = mock.MagicMock(url="https://app.databricksapps.com/")
    ws_ctor = mock.MagicMock(return_value=fake_ws)
    with mock.patch("databricks.sdk.WorkspaceClient", ws_ctor):
        result = call_mod.resolve_app_host(instance="demo", target="dev", project_root=root, **kwargs)
    fake_ws.apps.get.assert_called_once_with("dev-demo-app")
    return result, ws_ctor


class TestResolveAppHost:
    def test_app_host_override_returns_profile_unchanged(self) -> None:
        host, profile = call_mod.resolve_app_host(instance=None, app_host="my.databricksapps.com", profile="p")
        assert host == "my.databricksapps.com"
        assert profile == "p"

    def test_uses_foundry_toml_target_profile(self, tmp_path: Path) -> None:
        _write_repo(tmp_path, "my-profile")

        (host, profile), ws_ctor = _resolve(tmp_path)

        assert host == "app.databricksapps.com"
        assert profile == "my-profile"
        ws_ctor.assert_called_once_with(profile="my-profile", **product_kwargs())

    def test_explicit_profile_wins_over_foundry_toml(self, tmp_path: Path) -> None:
        _write_repo(tmp_path, "toml-profile")

        (_host, profile), ws_ctor = _resolve(tmp_path, profile="cli-profile")

        assert profile == "cli-profile"
        ws_ctor.assert_called_once_with(profile="cli-profile", **product_kwargs())

    def test_undeclared_instance_raises_value_error(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="--app-host"):
            call_mod.resolve_app_host(instance="demo", project_root=tmp_path)

    def test_no_instance_and_no_host_raises(self) -> None:
        with pytest.raises(ValueError):
            call_mod.resolve_app_host(instance=None)
