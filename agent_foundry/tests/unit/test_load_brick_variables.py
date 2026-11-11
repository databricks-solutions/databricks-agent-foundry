"""Tests for ``agent_foundry.bricks.load_brick_variables`` (#191, F-PR6).

Every brick standalone-CLI entrypoint uses this helper to load its
``${var.*}`` bag from ``foundry.toml`` via :func:`load_instance_config`.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from agent_foundry.bricks import fill_state_outputs, load_brick_variables
from agent_foundry.state import save_state
from agent_foundry.testing.fakes import FakeWorkspaceClient

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_foundry_toml_repo(
    repo: Path,
    *,
    instance_name: str = "demo",
    profile: str = "dev-profile",
    warehouse_id: str | None = "wh-dev-123",
) -> Path:
    """Write a schema-v2 repo tree and return the instance directory.

    Pass ``warehouse_id=None`` to omit the ``[warehouse]`` block, which is
    what a project looks like when the warehouse brick provisions the
    warehouse instead of the config pinning an existing one.
    """
    (repo / "foundry.toml").write_text(
        textwrap.dedent(
            f"""
            schema_version = 2

            [targets.dev]
            host = "https://dev.cloud.databricks.com"
            catalog = "main"
            profile = "{profile}"

            [[instances]]
            path = "instances/{instance_name}"
            """
        ).lstrip(),
        encoding="utf-8",
    )
    inst = repo / "instances" / instance_name
    inst.mkdir(parents=True)
    instance_toml = textwrap.dedent(
        f"""
        name = "{instance_name}"
        schema_prefix = "{instance_name}_app"
        vs_endpoint_name = "vs-{instance_name}"
        app_name = "{instance_name}-app"
        """
    ).lstrip()
    if warehouse_id is not None:
        instance_toml += textwrap.dedent(
            f"""
            [warehouse]
            mode = "reuse"
            warehouse_id = "{warehouse_id}"
            """
        )
    (inst / "foundry.toml").write_text(instance_toml, encoding="utf-8")
    return inst


def _workspace_with_warehouse_state(instance_dir: Path, target: str, warehouse_id: str) -> FakeWorkspaceClient:
    """Record a provisioned warehouse the way the warehouse brick does."""
    (instance_dir / "warehouse").mkdir(parents=True, exist_ok=True)
    ws = FakeWorkspaceClient()
    # ``WarehouseState`` requires both keys, and load_state_outputs validates against it.
    save_state(
        ws, instance_dir.name, "warehouse", instance_dir.name, target, {"warehouse_id": warehouse_id, "name": "demo-wh"}
    )
    return ws


# ---------------------------------------------------------------------------
# foundry.toml path
# ---------------------------------------------------------------------------


def test_foundry_toml_path_returns_to_variables_bag(tmp_path: Path) -> None:
    """When foundry.toml is present, the helper must return the
    InstanceConfig.to_variables() shape -- catalog from the target
    block, schema_prefix from the per-instance file, derived fields
    like agent_schema and solution_root.
    """
    instance_dir = _write_foundry_toml_repo(tmp_path)

    bag = load_brick_variables(
        tmp_path,
        target="dev",
        instance_dir=instance_dir,
    )

    assert bag["catalog"] == "main"
    assert bag["schema_prefix"] == "demo_app"
    assert bag["app_name"] == "demo-app"
    assert bag["agent_schema"] == "main.demo_app_agent"
    assert "solution_root" in bag


def test_foundry_toml_path_preserves_target_identity_fields(tmp_path: Path) -> None:
    """Schema-v2 target fields (profile, warehouse_id, tracing_destination)
    must survive ``load_brick_variables`` (#394).
    """
    instance_dir = _write_foundry_toml_repo(tmp_path)

    bag = load_brick_variables(
        tmp_path,
        target="dev",
        instance_dir=instance_dir,
    )

    assert bag["profile"] == "dev-profile"
    assert bag["warehouse_id"] == "wh-dev-123"
    assert bag["tracing_destination"] == "main.demo_app_agent"


def test_foundry_toml_path_folds_in_extra_vars(tmp_path: Path) -> None:
    """``--vars KEY=VALUE`` extras must land in the final bag (matches
    F-PR5's deploy-verb precedence: extras win over to_variables()).
    """
    instance_dir = _write_foundry_toml_repo(tmp_path)

    bag = load_brick_variables(
        tmp_path,
        target="dev",
        instance_dir=instance_dir,
        extra_vars=["custom_key=hello", "catalog=overridden"],
    )

    assert bag["custom_key"] == "hello"
    # Overrides win over the typed default.
    assert bag["catalog"] == "overridden"


def test_foundry_toml_path_does_not_warn(tmp_path: Path, recwarn: pytest.WarningsRecorder) -> None:
    """The new path is the recommended state; calling the helper
    against a foundry.toml repo must not emit DeprecationWarning.
    """
    instance_dir = _write_foundry_toml_repo(tmp_path)

    load_brick_variables(tmp_path, target="dev", instance_dir=instance_dir)

    deprecations = [w for w in recwarn.list if issubclass(w.category, DeprecationWarning)]
    assert deprecations == []


def test_foundry_toml_path_raises_value_error_on_loader_failure(
    tmp_path: Path,
) -> None:
    """Loader errors (unknown target etc.) are re-raised as plain
    ValueError so brick scripts can keep their existing ``except``
    blocks without depending on the loader module.
    """
    instance_dir = _write_foundry_toml_repo(tmp_path)

    with pytest.raises(ValueError, match="staging"):
        load_brick_variables(tmp_path, target="staging", instance_dir=instance_dir)


# ---------------------------------------------------------------------------
# Deploy-time state outputs (#416)
# ---------------------------------------------------------------------------
#
# A deploy reaches these via ``_preload_state``.  A brick invoked through
# its own CLI goes through ``load_brick_variables`` instead, which used to
# skip persisted state entirely -- so ``sql_function deploy`` failed with
# "warehouse_id variable is required" against a project whose warehouse was
# provisioned by an earlier run.


def test_warehouse_id_is_read_from_state_when_the_config_pins_none(
    tmp_path: Path,
) -> None:
    instance_dir = _write_foundry_toml_repo(tmp_path, warehouse_id=None)
    ws = _workspace_with_warehouse_state(instance_dir, "dev", "wh-from-state")

    bag = load_brick_variables(tmp_path, target="dev", instance_dir=instance_dir, ws=ws)

    assert bag["warehouse_id"] == "wh-from-state"


def test_state_is_not_read_without_a_workspace_client(tmp_path: Path) -> None:
    instance_dir = _write_foundry_toml_repo(tmp_path, warehouse_id=None)
    _workspace_with_warehouse_state(instance_dir, "dev", "wh-from-state")

    bag = load_brick_variables(tmp_path, target="dev", instance_dir=instance_dir)

    assert not bag.get("warehouse_id")


def test_nested_brick_state_is_exposed_per_sub_instance(tmp_path: Path) -> None:
    """Genie and vector-search outputs are keyed by sub-instance, and a
    standalone brick run needs them for the same cross-references a
    deploy makes."""
    instance_dir = _write_foundry_toml_repo(tmp_path, warehouse_id=None)
    (instance_dir / "genie_spaces" / "info_schema").mkdir(parents=True)
    ws = FakeWorkspaceClient()
    save_state(
        ws, instance_dir.name, "genie_spaces", "info_schema", "dev", {"space_id": "sp-123", "title": "Info Schema"}
    )

    bag = load_brick_variables(tmp_path, target="dev", instance_dir=instance_dir, ws=ws)

    assert bag["genie_space_id__info_schema"] == "sp-123"


def test_explicit_vars_beat_persisted_state(tmp_path: Path) -> None:
    """Deliberate divergence from ``_preload_state``, which overrides even
    an explicitly passed value: on a debugging escape hatch the value the
    caller typed must win."""
    instance_dir = _write_foundry_toml_repo(tmp_path, warehouse_id=None)
    ws = _workspace_with_warehouse_state(instance_dir, "dev", "wh-from-state")

    bag = load_brick_variables(
        tmp_path,
        target="dev",
        instance_dir=instance_dir,
        extra_vars=["warehouse_id=wh-explicit"],
        ws=ws,
    )

    assert bag["warehouse_id"] == "wh-explicit"


def test_a_pinned_warehouse_id_is_not_replaced_by_state(tmp_path: Path) -> None:
    instance_dir = _write_foundry_toml_repo(tmp_path, warehouse_id="wh-pinned")
    ws = _workspace_with_warehouse_state(instance_dir, "dev", "wh-from-state")

    bag = load_brick_variables(tmp_path, target="dev", instance_dir=instance_dir, ws=ws)

    assert bag["warehouse_id"] == "wh-pinned"


def test_state_from_another_target_is_ignored(tmp_path: Path) -> None:
    instance_dir = _write_foundry_toml_repo(tmp_path, warehouse_id=None)
    ws = _workspace_with_warehouse_state(instance_dir, "prod", "wh-prod")

    bag = load_brick_variables(tmp_path, target="dev", instance_dir=instance_dir, ws=ws)

    assert not bag.get("warehouse_id")


def test_fill_after_client_construction_adds_recorded_outputs(tmp_path: Path) -> None:
    """The brick-CLI order (#598): load without a client, build it, then fill."""
    instance_dir = _write_foundry_toml_repo(tmp_path, warehouse_id=None)
    ws = _workspace_with_warehouse_state(instance_dir, "dev", "wh-from-state")
    bag = load_brick_variables(tmp_path, target="dev", instance_dir=instance_dir)

    filled = fill_state_outputs(bag, instance_dir, "dev", ws)

    assert filled["warehouse_id"] == "wh-from-state"
    assert filled["catalog"] == "main"


def test_fill_without_a_client_leaves_the_bag_unchanged(tmp_path: Path) -> None:
    instance_dir = _write_foundry_toml_repo(tmp_path, warehouse_id=None)
    bag = load_brick_variables(tmp_path, target="dev", instance_dir=instance_dir)

    assert fill_state_outputs(dict(bag), instance_dir, "dev", None) == bag


def test_missing_state_still_returns_a_usable_bag(tmp_path: Path) -> None:
    """No state yet (first run) is normal, not an error -- the brick
    reports what it actually needs."""
    instance_dir = _write_foundry_toml_repo(tmp_path, warehouse_id=None)

    bag = load_brick_variables(tmp_path, target="dev", instance_dir=instance_dir)

    assert bag["catalog"] == "main"
    assert not bag.get("warehouse_id")


def test_bag_is_dict_of_str(tmp_path: Path) -> None:
    """Brick scripts assume ``dict[str, str]`` -- no Path objects or ints."""
    instance_dir = _write_foundry_toml_repo(tmp_path)

    bag = load_brick_variables(tmp_path, target="dev", instance_dir=instance_dir)

    for k, v in bag.items():
        assert isinstance(k, str), f"non-str key: {k!r}"
        assert isinstance(v, str), f"non-str value for {k!r}: {v!r}"


def test_missing_foundry_toml_raises_value_error(tmp_path: Path) -> None:
    instance_dir = tmp_path / "instances" / "demo"
    instance_dir.mkdir(parents=True)

    with pytest.raises(ValueError, match="foundry.toml"):
        load_brick_variables(tmp_path, target="dev", instance_dir=instance_dir)
