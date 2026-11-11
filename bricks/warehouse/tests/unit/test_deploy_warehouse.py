"""Tests for the warehouse deploy brick (offline -- no workspace needed)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import yaml

from agent_foundry import state as shared_state
from agent_foundry.testing.fakes import FakeWorkspaceClient

if TYPE_CHECKING:
    from agent_foundry.bricks import DeployContext

BRICK_DIR = Path(__file__).resolve().parents[2]


class TestConfigLoading:
    """Validate config loading and variable substitution."""

    def _write_config(self, instance_dir: Path, config: dict[str, Any]) -> None:
        wh_dir = instance_dir / "warehouse"
        wh_dir.mkdir(parents=True, exist_ok=True)
        (wh_dir / "config.yml").write_text(yaml.dump(config), encoding="utf-8")

    def test_load_config_substitutes_variables(self, tmp_path: Path) -> None:
        from bricks.warehouse.scripts.deploy_warehouse import load_config

        self._write_config(
            tmp_path,
            {
                "name": "${var.app_name}-warehouse",
                "size": "2X-SMALL",
                "tags": {"app": "${var.app_name}"},
            },
        )
        config = load_config(tmp_path, {"app_name": "my-app"})
        assert config["name"] == "my-app-warehouse"
        assert config["tags"]["app"] == "my-app"
        assert config["tags"]["ai_solution_managed_by"] == "my-app"

    def test_load_config_managed_by_wins_over_user_tag(self, tmp_path: Path) -> None:
        from bricks.warehouse.scripts.deploy_warehouse import load_config

        self._write_config(
            tmp_path,
            {
                "name": "wh",
                "size": "2X-SMALL",
                "tags": {"ai_solution_managed_by": "spoof", "team": "fe"},
            },
        )
        config = load_config(tmp_path, {"app_name": "real-app"})
        assert config["tags"]["ai_solution_managed_by"] == "real-app"
        assert config["tags"]["team"] == "fe"

    def test_load_config_skips_managed_by_when_app_name_empty(self, tmp_path: Path) -> None:
        from bricks.warehouse.scripts.deploy_warehouse import load_config

        self._write_config(tmp_path, {"name": "wh", "size": "2X-SMALL", "tags": {"team": "fe"}})
        config = load_config(tmp_path, {"app_name": ""})
        assert "ai_solution_managed_by" not in config["tags"]
        assert config["tags"]["team"] == "fe"

    def test_load_config_missing_raises(self, tmp_path: Path) -> None:
        from bricks.warehouse.scripts.deploy_warehouse import load_config

        with pytest.raises(FileNotFoundError):
            load_config(tmp_path, {})

    def test_load_config_unresolved_vars_left_as_is(self, tmp_path: Path) -> None:
        from bricks.warehouse.scripts.deploy_warehouse import load_config

        self._write_config(
            tmp_path,
            {
                "name": "${var.app_name}-warehouse",
                "size": "2X-SMALL",
            },
        )
        config = load_config(tmp_path, {})
        assert "${var.app_name}" in config["name"]


def _instance_with_config(tmp_path: Path) -> Path:
    instance_dir = tmp_path / "instances" / "demo"
    wh_dir = instance_dir / "warehouse"
    wh_dir.mkdir(parents=True)
    (wh_dir / "config.yml").write_text(yaml.dump({"name": "demo-wh", "size": "2X-SMALL"}), encoding="utf-8")
    return instance_dir


class TestStateManagement:
    """State lives in the shared workspace state (agent_foundry.state), not a local file."""

    def test_state_roundtrip_is_workspace_backed(self, tmp_path: Path) -> None:
        from bricks.warehouse.scripts.deploy_warehouse import load_state, save_state

        ws = FakeWorkspaceClient()
        instance_dir = tmp_path / "demo"
        assert load_state(ws, instance_dir, "dev") == {}
        save_state(ws, instance_dir, "dev", {"warehouse_id": "abc123", "name": "test-wh"}, action="create", deployer="")

        state = shared_state.load_state(ws, "demo", "warehouse", "demo", "dev")
        assert (state["warehouse_id"], state["name"]) == ("abc123", "test-wh")
        assert not (instance_dir / "warehouse" / "_state").exists()

    def test_destroy_from_shared_state_on_a_fresh_machine(self, tmp_path: Path) -> None:
        from bricks.warehouse.scripts.deploy_warehouse import deploy

        instance_dir = _instance_with_config(tmp_path)
        ws = FakeWorkspaceClient()
        shared_state.save_state(ws, "demo", "warehouse", "demo", "dev", {"warehouse_id": "wh-1", "name": "demo-wh"})

        assert deploy(instance_dir, "dev", {}, action="destroy", ws=ws) == 0

        assert ("DELETE", "/api/2.0/sql/warehouses/wh-1", None) in ws.api_client.calls
        assert shared_state.load_state(ws, "demo", "warehouse", "demo", "dev") == {}

    def test_brick_publishes_warehouse_id_from_shared_state(self, tmp_path: Path) -> None:
        from agent_foundry.bricks import DeployContext
        from bricks.warehouse.brick import SqlWarehouseBrick

        instance_dir = _instance_with_config(tmp_path)
        ws = FakeWorkspaceClient()
        shared_state.save_state(ws, "demo", "warehouse", "demo", "dev", {"warehouse_id": "wh-1", "name": "demo-wh"})

        outputs = SqlWarehouseBrick().load_state_outputs(DeployContext(instance_dir=instance_dir, target="dev", ws=ws))

        assert outputs == {"warehouse_id": "wh-1"}


class TestBuildTags:
    """Validate tag format conversion."""

    def test_build_tags_format(self) -> None:
        from bricks.warehouse.scripts.deploy_warehouse import _build_tags

        result = _build_tags({"app": "my-app", "env": "dev"})
        assert "custom_tags" in result
        tags = result["custom_tags"]
        assert len(tags) == 2
        assert {"key": "app", "value": "my-app"} in tags
        assert {"key": "env", "value": "dev"} in tags

    def test_build_tags_empty(self) -> None:
        from bricks.warehouse.scripts.deploy_warehouse import _build_tags

        result = _build_tags({})
        assert result == {"custom_tags": []}


class TestDeployDryRun:
    """Validate deploy in dry-run mode (no API calls)."""

    def _write_config(self, instance_dir: Path, config: dict[str, Any]) -> None:
        wh_dir = instance_dir / "warehouse"
        wh_dir.mkdir(parents=True, exist_ok=True)
        (wh_dir / "config.yml").write_text(yaml.dump(config), encoding="utf-8")

    def test_deploy_dry_run_returns_zero(self, tmp_path: Path) -> None:
        from bricks.warehouse.scripts.deploy_warehouse import deploy

        self._write_config(
            tmp_path,
            {
                "name": "test-warehouse",
                "size": "2X-SMALL",
                "auto_stop_mins": 10,
                "tags": {"app": "test-app"},
            },
        )
        rc = deploy(tmp_path, "dev", {"app_name": "test-app"}, dry_run=True)
        assert rc == 0

    def test_deploy_no_config_returns_zero(self, tmp_path: Path) -> None:
        """No warehouse config means the brick is a no-op."""
        from bricks.warehouse.scripts.deploy_warehouse import deploy

        rc = deploy(tmp_path, "dev", {}, dry_run=True)
        assert rc == 0

    def test_deploy_destroy_no_state(self, tmp_path: Path) -> None:
        from bricks.warehouse.scripts.deploy_warehouse import deploy

        self._write_config(tmp_path, {"name": "test-wh", "size": "2X-SMALL"})
        rc = deploy(tmp_path, "dev", {}, action="destroy", dry_run=True)
        assert rc == 0


class TestModuleImportable:
    """Verify the module can be imported."""

    def test_deploy_module_importable(self) -> None:
        from bricks.warehouse.scripts import deploy_warehouse as mod

        assert hasattr(mod, "deploy")
        assert hasattr(mod, "load_config")
        assert hasattr(mod, "load_state")
        assert hasattr(mod, "save_state")

    def test_state_key_matches_the_brick_reader(self) -> None:
        from bricks.warehouse.brick import SqlWarehouseBrick
        from bricks.warehouse.scripts.deploy_warehouse import BRICK_TYPE

        assert BRICK_TYPE == SqlWarehouseBrick.state_subdir


class TestAppliesTo:
    """``warehouse/config.yml`` decides whether the brick runs.

    A ``mode = "reuse"`` instance never has that file (the loader rejects
    the combination), so the brick skips it without reading foundry.toml.
    """

    def _make_ctx(self, instance_dir: Path) -> "DeployContext":
        from agent_foundry.bricks import DeployContext

        return DeployContext(
            instance_dir=instance_dir,
            target="dev",
            variables={},
            dry_run=True,
        )

    def _write_warehouse_yaml(self, instance_dir: Path) -> None:
        wh_dir = instance_dir / "warehouse"
        wh_dir.mkdir(parents=True, exist_ok=True)
        (wh_dir / "config.yml").write_text("size: 'Small'\n", encoding="utf-8")

    def test_applies_when_config_yml_present(self, tmp_path: Path) -> None:
        from bricks.warehouse.brick import SqlWarehouseBrick

        self._write_warehouse_yaml(tmp_path)
        assert SqlWarehouseBrick().applies_to(self._make_ctx(tmp_path)) is True

    def test_skips_without_config_yml(self, tmp_path: Path) -> None:
        from bricks.warehouse.brick import SqlWarehouseBrick

        assert SqlWarehouseBrick().applies_to(self._make_ctx(tmp_path)) is False
