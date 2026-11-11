"""Tests for agent_foundry.config -- Pydantic configuration models."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_foundry.config import (
    TAG_KEY_MANAGED_BY,
    DeploymentState,
    EvaluationConfig,
    InstanceConfig,
    TargetConfig,
    WarehouseConfig,
    load_state_outputs,
    merge_managed_by_tags,
)
from agent_foundry.state import save_state
from agent_foundry.testing.fakes import FakeWorkspaceClient

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def instance_dir(tmp_path: Path) -> Path:
    inst = tmp_path / "instances" / "test-app"
    inst.mkdir(parents=True)
    return inst


# ---------------------------------------------------------------------------
# Model construction
# ---------------------------------------------------------------------------


class TestInstanceConfigBasic:
    def test_minimal_construction(self):
        cfg = InstanceConfig(name="my-app", schema_prefix="my_app")
        assert cfg.name == "my-app"
        assert cfg.app_name == "my-app"  # defaulted from name
        assert cfg.schema_prefix == "my_app"

    def test_app_name_defaults_to_name(self):
        cfg = InstanceConfig(name="hello", schema_prefix="hello")
        assert cfg.app_name == "hello"

    def test_explicit_app_name_preserved(self):
        cfg = InstanceConfig(name="hello", schema_prefix="hello", app_name="custom")
        assert cfg.app_name == "custom"

    def test_missing_required_field_raises(self):
        with pytest.raises(Exception):
            # Intentional negative test: ``InstanceConfig.schema_prefix``
            # is required at runtime; mypy's ``call-arg`` complaint is
            # the static mirror of the same constraint.
            InstanceConfig(name="oops")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Computed fields
# ---------------------------------------------------------------------------


class TestComputedFields:
    @pytest.fixture()
    def cfg(self) -> InstanceConfig:
        return InstanceConfig(
            name="my-test-app",
            schema_prefix="my_test_app",
            app_name="my-test-app",
            target=TargetConfig(
                catalog="main",
                deployer="user@example.com",
            ),
        )

    def test_agent_schema(self, cfg: InstanceConfig):
        assert cfg.agent_schema == "main.my_test_app_agent"

    def test_agent_schema_name_drops_the_catalog(self, cfg: InstanceConfig):
        """SQL templates need the parts separately so each can be back-quoted."""
        assert cfg.agent_schema_name == "my_test_app_agent"
        assert cfg.to_variables()["agent_schema_name"] == "my_test_app_agent"

    def test_tracing_destination(self, cfg: InstanceConfig):
        assert cfg.tracing_destination == "main.my_test_app_agent"

    def test_tracing_experiment(self, cfg: InstanceConfig):
        expected = "/Shared/agent-foundry/dev/my-test-app/experiments/dev-my-test-app-traces"
        assert cfg.tracing_experiment == expected

    def test_tracing_table_prefix_is_constant(self, cfg: InstanceConfig):
        assert cfg.tracing_table_prefix == "mlflow_experiment"

    def test_tracing_uc_destination(self, cfg: InstanceConfig):
        assert cfg.tracing_uc_destination == "main.my_test_app_agent.mlflow_experiment"

    def test_warehouse_display_name(self, cfg: InstanceConfig):
        assert cfg.warehouse_display_name == "[dev] my-test-app-warehouse"

    def test_dashboard_display_name(self, cfg: InstanceConfig):
        assert cfg.dashboard_display_name == "[dev] my-test-app-dashboard"

    def test_genie_display_prefix(self, cfg: InstanceConfig):
        assert cfg.genie_display_prefix == "[dev] my-test-app"

    def test_app_resource_name(self, cfg: InstanceConfig):
        assert cfg.app_resource_name == "dev-my-test-app-app"

    def test_chat_ui_resource_name(self, cfg: InstanceConfig):
        assert cfg.chat_ui_resource_name == "dev-my-test-app-app-ui"

    def test_solution_root_includes_target(self, cfg: InstanceConfig):
        assert cfg.solution_root == "/Shared/agent-foundry/dev/my-test-app"


# ---------------------------------------------------------------------------
# to_variables bridge
# ---------------------------------------------------------------------------
#
# The framework variable bag is agent-agnostic: it carries identity /
# target / tracing / naming only.  Agent specifics (LLM endpoint,
# Lakebase, chat-history / file-upload toggles) are contributed by the
# agent payload (see payloads/agent tests), NOT by ``to_variables``.


class TestToVariables:
    def test_round_trip_basic(self):
        cfg = InstanceConfig(
            name="my-app",
            schema_prefix="my_app",
            target=TargetConfig(
                host="https://host.net",
                catalog="main",
                deployer="me@example.com",
            ),
        )
        v = cfg.to_variables()
        assert v["host"] == "https://host.net"
        assert v["catalog"] == "main"
        assert v["app_name"] == "my-app"
        assert v["schema_prefix"] == "my_app"
        assert v["tracing_destination"] == "main.my_app_agent"
        assert v["tracing_experiment"] == "/Shared/agent-foundry/dev/my-app/experiments/dev-my-app-traces"
        assert v["tracing_table_prefix"] == "mlflow_experiment"
        assert v["target_name"] == "dev"
        assert v["warehouse_display_name"] == "[dev] my-app-warehouse"
        assert v["dashboard_display_name"] == "[dev] my-app-dashboard"
        assert v["app_resource_name"] == "dev-my-app-app"

    def test_no_agent_keys_in_bag(self):
        """The agent-agnostic framework must not leak agent specifics."""
        cfg = InstanceConfig(name="app", schema_prefix="app")
        v = cfg.to_variables()
        for leaked in (
            "llm_endpoint_name",
            "lakebase_project_name",
            "chat_history_enabled",
            "file_upload_enabled",
            "file_upload_max_mb",
        ):
            assert leaked not in v, f"{leaked} must be contributed by the agent payload, not the framework"

    def test_state_overrides_warehouse_id(self):
        cfg = InstanceConfig(
            name="app",
            schema_prefix="app",
            target=TargetConfig(warehouse_id="old-wh"),
        )
        state = DeploymentState(warehouse_id="new-wh-from-deploy")
        v = cfg.to_variables(state)
        assert v["warehouse_id"] == "new-wh-from-deploy"

    def test_state_adds_genie_space_id(self):
        cfg = InstanceConfig(name="app", schema_prefix="app")
        state = DeploymentState(genie_space_id="abc123")
        v = cfg.to_variables(state)
        assert v["genie_space_id"] == "abc123"

    def test_state_none_values_not_added(self):
        cfg = InstanceConfig(name="app", schema_prefix="app")
        state = DeploymentState()
        v = cfg.to_variables(state)
        assert "genie_space_id" not in v
        assert "experiment_id" not in v

    def test_all_values_are_strings(self):
        cfg = InstanceConfig(
            name="app",
            schema_prefix="app",
            target=TargetConfig(catalog="c", deployer="d"),
        )
        v = cfg.to_variables()
        for key, val in v.items():
            assert isinstance(val, str), f"{key} is {type(val)}, expected str"


# ---------------------------------------------------------------------------
# State loading
# ---------------------------------------------------------------------------


def _seed_state(
    ws: FakeWorkspaceClient,
    instance_dir: Path,
    brick_dir: str,
    sub_instance: str | None,
    state: dict[str, str],
) -> None:
    """Create the brick's authoring dir and write its workspace state for ``dev``."""
    (instance_dir / brick_dir / (sub_instance or "")).mkdir(parents=True, exist_ok=True)
    save_state(ws, instance_dir.name, brick_dir, sub_instance or instance_dir.name, "dev", state)


class TestLoadStateOutputs:
    def test_loads_warehouse_state(self, instance_dir: Path):
        ws = FakeWorkspaceClient()
        _seed_state(ws, instance_dir, "warehouse", None, {"warehouse_id": "wh-12345", "name": "test-warehouse"})
        result = load_state_outputs(instance_dir, "dev", ws=ws)
        assert result.variables["warehouse_id"] == "wh-12345"

    def test_loads_genie_state_per_sub_instance(self, instance_dir: Path):
        ws = FakeWorkspaceClient()
        _seed_state(ws, instance_dir, "genie_spaces", "info_schema", {"space_id": "sp-999", "title": "Info Schema"})
        result = load_state_outputs(instance_dir, "dev", ws=ws)
        assert result.variables["genie_space_id__info_schema"] == "sp-999"
        assert "genie_space_id" not in result.variables

    def test_loads_vector_search_state_per_sub_instance(self, instance_dir: Path):
        ws = FakeWorkspaceClient()
        _seed_state(
            ws,
            instance_dir,
            "vector_indexes",
            "docs",
            {"index_name": "main.idx.docs", "endpoint_name": "vs-ep", "index_type": "DELTA_SYNC"},
        )
        result = load_state_outputs(instance_dir, "dev", ws=ws)
        assert result.variables["vs_index_name__docs"] == "main.idx.docs"
        assert result.variables["vs_endpoint_name__docs"] == "vs-ep"
        assert "vs_index_name" not in result.variables
        assert "vs_endpoint_name" not in result.variables

    def test_two_genie_sub_instances_no_collision(self, instance_dir: Path):
        """Regression guard for issue #50: two sub-instances must not
        overwrite each other's IDs via alphabetical iteration."""
        ws = FakeWorkspaceClient()
        for sub, sid in [
            ("info_schema", "sp-info"),
            ("ask_swf_portfolio_planning", "sp-ask"),
        ]:
            _seed_state(ws, instance_dir, "genie_spaces", sub, {"space_id": sid, "title": sub})

        result = load_state_outputs(instance_dir, "dev", ws=ws)
        assert result.variables["genie_space_id__info_schema"] == "sp-info"
        assert result.variables["genie_space_id__ask_swf_portfolio_planning"] == "sp-ask"
        assert "genie_space_id" not in result.variables, "no flat key should be emitted for nested layouts"

    def test_missing_state_returns_empty(self, instance_dir: Path):
        result = load_state_outputs(instance_dir, "dev", ws=FakeWorkspaceClient())
        assert result.variables == {}

    def test_wrong_target_returns_empty(self, instance_dir: Path):
        ws = FakeWorkspaceClient()
        _seed_state(ws, instance_dir, "warehouse", None, {"warehouse_id": "wh-1", "name": "wh"})
        result = load_state_outputs(instance_dir, "staging", ws=ws)
        assert result.variables == {}

    def test_offline_ignores_local_state_files(self, instance_dir: Path):
        """Without a workspace client there is no state; leftover local files are not read."""
        state_dir = instance_dir / "warehouse" / "_state"
        state_dir.mkdir(parents=True)
        (state_dir / "dev.json").write_text(json.dumps({"warehouse_id": "wh-local", "name": "wh"}))
        assert load_state_outputs(instance_dir, "dev").variables == {}


# ---------------------------------------------------------------------------
# Registry-driven state aggregation
# ---------------------------------------------------------------------------
#
# Post-68-C (#84), state-key mappings live on each Brick rather than in
# a central ``STATE_KEY_MAPS`` dict.  These tests assert that every
# previously-mapped brick is represented by a registered ``Brick`` and
# that ``load_state_outputs`` still aggregates across the full set.


class TestStateAggregationAcrossRegistry:
    def test_every_stateful_brick_has_a_registered_implementation(self):
        from agent_foundry import iter_bricks

        registered = {b.name for b in iter_bricks()}
        expected = {
            "warehouse",
            # ``agent`` was renamed to ``tracing`` (#338): it only ever
            # provisioned the MLflow tracing experiment.
            "tracing",
            # ``lakebase`` was folded into the App brick (see CHANGELOG);
            # the ``app`` brick now publishes the lakebase_* state
            # keys via its own load_state_outputs.
            "app",
            "genie_spaces",
            "vector_indexes",
        }
        missing = expected - registered
        assert not missing, f"Registry missing stateful bricks: {missing!r}"

    def test_flat_layout_warehouse_maps_warehouse_id(self, tmp_path: Path):
        inst = tmp_path / "app"
        ws = FakeWorkspaceClient()
        # #156: warehouse state must satisfy WarehouseState (warehouse_id AND name).
        _seed_state(ws, inst, "warehouse", None, {"warehouse_id": "wh-123", "name": "test-wh"})
        result = load_state_outputs(inst, "dev", ws=ws)
        assert result.variables.get("warehouse_id") == "wh-123"

    def test_nested_layout_genie_prefixes_with_sub_instance(self, tmp_path: Path):
        inst = tmp_path / "app"
        ws = FakeWorkspaceClient()
        _seed_state(ws, inst, "genie_spaces", "my_space", {"space_id": "sp-abc", "title": "My Space"})
        result = load_state_outputs(inst, "dev", ws=ws)
        assert result.variables.get("genie_space_id__my_space") == "sp-abc"


# ---------------------------------------------------------------------------
# Sub-model defaults
# ---------------------------------------------------------------------------


class TestSubModelDefaults:
    def test_warehouse_config_defaults(self):
        w = WarehouseConfig()
        assert w.size == "2X-Small"
        assert w.auto_stop_mins == 10

    def test_evaluation_config_defaults(self):
        e = EvaluationConfig()
        assert e.max_traces == 100
        assert e.scorers == {}

    def test_deployment_state_all_none(self):
        s = DeploymentState()
        assert s.warehouse_id is None
        assert s.genie_space_id is None
        assert s.experiment_id is None


# ---------------------------------------------------------------------------
# Frozen model enforcement
# ---------------------------------------------------------------------------


class TestFrozenModels:
    def test_instance_config_is_frozen(self):
        cfg = InstanceConfig(name="app", schema_prefix="app")
        with pytest.raises(Exception):
            cfg.name = "changed"

    def test_deployment_state_is_mutable(self):
        s = DeploymentState()
        s.warehouse_id = "wh-1"
        assert s.warehouse_id == "wh-1"


class TestMergeManagedByTags:
    def test_pins_after_user_tags(self) -> None:
        tags = merge_managed_by_tags("my-app", {"env": "dev", TAG_KEY_MANAGED_BY: "spoof"})
        assert tags[TAG_KEY_MANAGED_BY] == "my-app"
        assert tags["env"] == "dev"

    def test_skips_when_app_name_blank(self) -> None:
        tags = merge_managed_by_tags("  ", {"env": "dev"})
        assert TAG_KEY_MANAGED_BY not in tags
        assert tags == {"env": "dev"}

    def test_skips_when_app_name_missing(self) -> None:
        assert merge_managed_by_tags(None, None) == {}
