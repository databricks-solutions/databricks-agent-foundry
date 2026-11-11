"""Disk-level tests for ``agent_foundry.load_*`` (issue #191, F-PR2).

Covers the two-file loader, its error modes and CLI override plumbing.
The model-level checks (``extra="forbid"``, audience boundary, etc.)
already live in ``test_foundry_config_models.py``.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from agent_foundry import (
    ConfigNotFoundError,
    FoundryConfigError,
    InstanceConfig,
    PartialMigrationError,
    load_instance_config,
    load_repo_config,
)

# ---------------------------------------------------------------------------
# Fixtures: build minimal repo trees in tmp_path
# ---------------------------------------------------------------------------


def _write(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dedent(contents).lstrip(), encoding="utf-8")


def _write_repo_toml(repo_root: Path, *, multi: bool = False) -> None:
    body = """
    schema_version = 2

    [targets.dev]
    host    = "https://workspace.cloud.databricks.com"
    catalog = "main"
    profile = "dev-profile"

    [[instances]]
    path = "instances/portfolio-assistant"
    """
    if multi:
        body += """
    [[instances]]
    path = "instances/other-agent"
    """
    _write(repo_root / "foundry.toml", body)


def _write_instance_toml(
    instance_dir: Path,
    *,
    name: str = "portfolio-assistant",
    warehouse_block: str = "",
) -> None:
    # NOTE: ``[chat_history]`` / ``[file_upload]`` blocks are no longer
    # part of the per-instance foundry.toml surface (hard cutover, 2.0).
    # The framework loader is payload-agnostic; UI toggles live in the
    # agent payload's spec (``payloads/agent/payload.yml``).
    _write(
        instance_dir / "foundry.toml",
        f"""
        name             = "{name}"
        description      = "test agent"
        schema_prefix    = "{name.replace("-", "_")}"
        vs_endpoint_name = "vs_demo"
        {warehouse_block}
        """,
    )


@pytest.fixture
def single_instance_repo(tmp_path: Path) -> Path:
    """A repo with one instance, both foundry.toml files present."""
    _write_repo_toml(tmp_path)
    _write_instance_toml(tmp_path / "instances" / "portfolio-assistant")
    return tmp_path


@pytest.fixture
def multi_instance_repo(tmp_path: Path) -> Path:
    """A repo with two instances declared at the root."""
    _write_repo_toml(tmp_path, multi=True)
    _write_instance_toml(tmp_path / "instances" / "portfolio-assistant")
    _write_instance_toml(tmp_path / "instances" / "other-agent", name="other-agent")
    return tmp_path


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_load_repo_config_returns_typed_model(single_instance_repo: Path) -> None:
    repo = load_repo_config(single_instance_repo)
    assert repo.schema_version == 2
    assert repo.targets["dev"].host == "https://workspace.cloud.databricks.com"
    assert len(repo.instances) == 1
    assert repo.instances[0].path == Path("instances/portfolio-assistant")


def test_load_instance_config_single_instance_auto_selects(
    single_instance_repo: Path,
) -> None:
    """When only one [[instances]] entry exists, ``instance=None`` is fine.

    This matches the F-PR5 CLI ergonomic where ``--instance`` becomes
    optional for single-instance repos.
    """
    cfg = load_instance_config(single_instance_repo, target_name="dev")
    assert isinstance(cfg, InstanceConfig)
    assert cfg.name == "portfolio-assistant"
    assert cfg.schema_prefix == "portfolio_assistant"
    assert cfg.target.host == "https://workspace.cloud.databricks.com"
    assert cfg.target.catalog == "main"
    # schema_version=2: warehouse_id is per-instance, not in the
    # target. With default [warehouse] mode = "dedicated", the runtime
    # target.warehouse_id starts empty and is populated by the
    # warehouse brick's state at deploy time.
    assert cfg.target.warehouse_id == ""
    assert cfg.vs_endpoint_name == "vs_demo"


def test_load_instance_config_multi_instance_requires_name(
    multi_instance_repo: Path,
) -> None:
    """Multi-instance repos must pass ``instance=`` to disambiguate."""
    with pytest.raises(FoundryConfigError, match="multiple instances"):
        load_instance_config(multi_instance_repo, target_name="dev")

    cfg = load_instance_config(
        multi_instance_repo,
        instance="other-agent",
        target_name="dev",
    )
    assert cfg.name == "other-agent"
    assert cfg.schema_prefix == "other_agent"


def test_load_instance_config_threads_target_into_runtime_model(
    single_instance_repo: Path,
) -> None:
    """The merged ``InstanceConfig.target`` reflects the chosen [targets.<env>]."""
    cfg = load_instance_config(single_instance_repo, target_name="dev")
    assert cfg.target_name == "dev"
    assert cfg.target.profile == "dev-profile"


def test_foundry_toml_rejects_chat_history_block(tmp_path: Path) -> None:
    """Hard cutover (2.0): UI toggles moved out of the framework
    authoring surface into the agent payload spec.  A leftover
    ``[chat_history]`` block in a per-instance foundry.toml must now
    fail loudly (``extra="forbid"`` on :class:`FoundryInstanceConfig`)
    rather than being silently threaded into the runtime model.
    """
    _write_repo_toml(tmp_path)
    _write(
        tmp_path / "instances" / "portfolio-assistant" / "foundry.toml",
        """
        name             = "portfolio-assistant"
        schema_prefix    = "portfolio_assistant"
        vs_endpoint_name = "vs_demo"

        [chat_history]
        enabled = true
        """,
    )
    with pytest.raises(Exception, match="chat_history"):
        load_instance_config(tmp_path, target_name="dev")


def test_foundry_toml_rejects_file_upload_block(tmp_path: Path) -> None:
    """Mirror of the chat_history guard: ``[file_upload]`` is no longer
    part of the per-instance foundry.toml surface and must be rejected.
    """
    _write_repo_toml(tmp_path)
    _write(
        tmp_path / "instances" / "portfolio-assistant" / "foundry.toml",
        """
        name             = "portfolio-assistant"
        schema_prefix    = "portfolio_assistant"
        vs_endpoint_name = "vs_demo"

        [file_upload]
        enabled     = true
        max_file_mb = 25
        """,
    )
    with pytest.raises(Exception, match="file_upload"):
        load_instance_config(tmp_path, target_name="dev")


def test_loader_is_payload_agnostic(tmp_path: Path) -> None:
    """The framework loader composes only identity / target / warehouse
    / evaluation.  Agent runtime config + UI toggles (chat history,
    file upload, ...) are contributed by the agent payload adapter, not
    the loader -- so the runtime :class:`InstanceConfig` carries no such
    attributes and ``to_variables()`` never emits the payload-owned
    keys.  Presence of a ``payloads/agent/payload.yml`` must not leak
    into the framework-composed config.
    """
    _write_repo_toml(tmp_path)
    inst = tmp_path / "instances" / "portfolio-assistant"
    _write_instance_toml(inst)
    # An authored payload spec on disk must be ignored by the loader.
    _write(
        inst / "payloads" / "agent" / "payload.yml",
        """
        payload:
          adapter: agent
          spec:
            ui:
              chat_history:
                enabled: false
                project_name: caps-lakebase
              file_upload:
                enabled: true
                max_mb: 25
        """,
    )
    cfg = load_instance_config(tmp_path, target_name="dev")

    # The payload-owned surfaces are not framework concerns.
    assert not hasattr(cfg, "chat_history")
    assert not hasattr(cfg, "file_upload")
    assert not hasattr(cfg, "agent")

    # ...and the legacy variable bag stays free of the keys the agent
    # payload adapter is now responsible for contributing.
    v = cfg.to_variables()
    for payload_owned_key in (
        "chat_history_enabled",
        "file_upload_enabled",
        "file_upload_max_mb",
        "lakebase_project_name",
        "llm_endpoint_name",
    ):
        assert payload_owned_key not in v, f"loader leaked payload-owned key {payload_owned_key!r}"


def test_cli_overrides_apply_to_target_only(single_instance_repo: Path) -> None:
    """``--vars host=...`` reaches the target layer; identity fields don't
    accept overrides (they're authored in the per-instance file).
    """
    cfg = load_instance_config(
        single_instance_repo,
        target_name="dev",
        extra_vars=["host=https://override.example", "catalog=cli_catalog"],
    )
    assert cfg.target.host == "https://override.example"
    assert cfg.target.catalog == "cli_catalog"
    # Identity fields are not overridden:
    assert cfg.schema_prefix == "portfolio_assistant"


# ---------------------------------------------------------------------------
# Error modes
# ---------------------------------------------------------------------------


def test_missing_repo_toml_raises_config_not_found(tmp_path: Path) -> None:
    with pytest.raises(ConfigNotFoundError, match="foundry.toml"):
        load_repo_config(tmp_path)


def test_missing_per_instance_toml_raises_partial_migration(tmp_path: Path) -> None:
    """Repo-root file present, but no per-instance file -> loud failure."""
    _write_repo_toml(tmp_path)
    # No instances/portfolio-assistant/foundry.toml on disk.
    with pytest.raises(PartialMigrationError, match="no foundry.toml exists"):
        load_instance_config(tmp_path, target_name="dev")


def test_unknown_target_raises(single_instance_repo: Path) -> None:
    with pytest.raises(FoundryConfigError, match="Target 'staging' not declared"):
        load_instance_config(single_instance_repo, target_name="staging")


def test_unknown_instance_raises(multi_instance_repo: Path) -> None:
    with pytest.raises(FoundryConfigError, match="not found in foundry.toml"):
        load_instance_config(
            multi_instance_repo,
            instance="ghost",
            target_name="dev",
        )


def test_repo_toml_unknown_key_rejected(tmp_path: Path) -> None:
    """``extra='forbid'`` propagates through the loader -- typos at the
    repo-root level fail on load_repo_config, not silently later.
    """
    _write(
        tmp_path / "foundry.toml",
        """
        schema_version = 2
        unknown_top_level_key = "oops"
        """,
    )
    with pytest.raises(Exception, match="unknown_top_level_key"):
        load_repo_config(tmp_path)


def test_instance_toml_unknown_key_rejected(single_instance_repo: Path) -> None:
    """Same rule at the per-instance level."""
    _write(
        single_instance_repo / "instances" / "portfolio-assistant" / "foundry.toml",
        """
        name = "portfolio-assistant"
        schema_prefix = "portfolio_assistant"
        ware_house_id = "leaked"
        """,
    )
    with pytest.raises(Exception, match="ware_house_id"):
        load_instance_config(single_instance_repo, target_name="dev")


# ---------------------------------------------------------------------------
# Public-API exposure
# ---------------------------------------------------------------------------


def test_loader_symbols_exposed_at_package_root() -> None:
    """Downstreams should be able to ``from agent_foundry import load_instance_config``."""
    import agent_foundry

    for name in (
        "ConfigNotFoundError",
        "FoundryConfigError",
        "PartialMigrationError",
        "load_instance_config",
        "load_repo_config",
    ):
        assert name in agent_foundry.__all__, f"{name} missing from agent_foundry.__all__"
        assert hasattr(agent_foundry, name)


# ---------------------------------------------------------------------------
# schema_version migration & per-instance warehouse plumbing (PR1)
# ---------------------------------------------------------------------------


def test_schema_version_1_rejected_with_migration_pointer(tmp_path: Path) -> None:
    """An old ``schema_version = 1`` repo must fail loudly with a
    pointer to the migration guide, not silently accept and
    half-resolve the warehouse_id.
    """
    _write(
        tmp_path / "foundry.toml",
        """
        schema_version = 1

        [targets.dev]
        host         = "https://example.com"
        catalog      = "main"
        warehouse_id = "wh-abc"
        """,
    )
    with pytest.raises(FoundryConfigError, match="schema_version"):
        load_repo_config(tmp_path)


def test_warehouse_reuse_mode_threads_id_into_runtime(tmp_path: Path) -> None:
    """In ``mode = "reuse"`` the per-instance ``warehouse_id`` flows
    through to the runtime ``InstanceConfig.target.warehouse_id`` so
    downstream bricks see it without waiting for a warehouse-brick
    state file (which never lands).
    """
    _write_repo_toml(tmp_path)
    _write_instance_toml(
        tmp_path / "instances" / "portfolio-assistant",
        warehouse_block='\n[warehouse]\nmode = "reuse"\nwarehouse_id = "wh-shared"\n',
    )
    cfg = load_instance_config(tmp_path, target_name="dev")
    assert cfg.target.warehouse_id == "wh-shared"


def test_warehouse_dedicated_mode_leaves_runtime_warehouse_id_empty(
    single_instance_repo: Path,
) -> None:
    """Default mode ("dedicated") -> the warehouse brick is responsible
    for publishing the id later; the loader must not invent one.
    """
    cfg = load_instance_config(single_instance_repo, target_name="dev")
    assert cfg.target.warehouse_id == ""


def test_warehouse_reuse_rejects_warehouse_config_yml(tmp_path: Path) -> None:
    """Strict-overlap: ``mode = "reuse"`` + ``warehouse/config.yml``
    is ambiguous (the YAML configures a warehouse the brick will
    never provision). Loader must reject.
    """
    _write_repo_toml(tmp_path)
    instance_dir = tmp_path / "instances" / "portfolio-assistant"
    _write_instance_toml(
        instance_dir,
        warehouse_block='\n[warehouse]\nmode = "reuse"\nwarehouse_id = "wh-shared"\n',
    )
    _write(instance_dir / "warehouse" / "config.yml", 'size: "Small"\n')
    with pytest.raises(FoundryConfigError, match='mode = "reuse"'):
        load_instance_config(tmp_path, target_name="dev")


def test_warehouse_dedicated_mode_allows_warehouse_config_yml(
    single_instance_repo: Path,
) -> None:
    """``mode = "dedicated"`` (default) is the case where
    ``warehouse/config.yml`` is *expected*.  No overlap error.
    """
    _write(
        single_instance_repo / "instances" / "portfolio-assistant" / "warehouse" / "config.yml",
        'size: "Small"\nauto_stop_mins: 10\n',
    )
    cfg = load_instance_config(single_instance_repo, target_name="dev")
    assert cfg.warehouse.size == "Small"
    assert cfg.warehouse.auto_stop_mins == 10


def test_target_with_legacy_warehouse_id_key_rejected(tmp_path: Path) -> None:
    """A repo that bumped ``schema_version`` to 2 but forgot to remove
    ``warehouse_id`` from ``[targets.<name>]`` must fail validation.
    """
    _write(
        tmp_path / "foundry.toml",
        """
        schema_version = 2

        [targets.dev]
        host         = "https://example.com"
        catalog      = "main"
        warehouse_id = "wh-abc"

        [[instances]]
        path = "instances/portfolio-assistant"
        """,
    )
    with pytest.raises(Exception, match="warehouse_id"):
        load_repo_config(tmp_path)
