"""Model-level tests for the typed ``foundry.toml`` contract (issue #191).

These tests cover only the Pydantic models added in F-PR1.  The disk
loader (``agent_foundry.load_config``) lands in F-PR2 and has its own
test file; here we exercise validation, defaults, and the
``extra="forbid"`` audience boundary on each model in isolation.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_foundry import (
    CURRENT_SCHEMA_VERSION,
    FoundryInstanceConfig,
    FoundryRepoConfig,
    InstanceRef,
    Target,
    Warehouse,
)

# ---------------------------------------------------------------------------
# Target
# ---------------------------------------------------------------------------


def test_target_defaults_are_empty_strings() -> None:
    """A bare ``Target()`` is valid -- the orchestrator (F-PR5) is what
    enforces "host + catalog required for a real deploy", not the
    model.  Dry-run validation and ``config show`` must still load
    partially-configured targets.
    """
    target = Target()
    assert target.host == ""
    assert target.catalog == ""
    assert target.schema_ == "analytics"


def test_target_rejects_warehouse_id() -> None:
    """``warehouse_id`` moved to per-instance ``[warehouse]`` in
    ``schema_version = 2``.  Authoring it at the repo-root target is
    a typo guard.
    """
    with pytest.raises(ValidationError, match="warehouse_id"):
        Target.model_validate({"warehouse_id": "abc123"})


def test_target_schema_alias_uses_user_facing_name() -> None:
    """The Pydantic attribute is ``schema_`` (trailing underscore so
    we don't shadow ``BaseModel.schema``), but the on-disk authoring
    key in ``foundry.toml`` is the natural ``schema``.  Only the
    alias is accepted on input -- ``extra="forbid"`` rejects the
    Python attribute name to keep the on-disk surface unambiguous.
    """
    via_alias = Target.model_validate({"schema": "marts"})
    assert via_alias.schema_ == "marts"

    with pytest.raises(ValidationError, match="schema_"):
        Target.model_validate({"schema_": "marts"})


def test_target_rejects_unknown_keys() -> None:
    """``extra="forbid"`` is the whole point of the typed contract --
    a typo like ``ware_house_id`` must surface at load time, not
    silently disappear into the default value.
    """
    with pytest.raises(ValidationError, match="ware_house_id"):
        Target.model_validate({"ware_house_id": "abc"})


# ---------------------------------------------------------------------------
# InstanceRef + FoundryRepoConfig
# ---------------------------------------------------------------------------


def test_instance_ref_only_carries_path() -> None:
    """Identity fields must not be authorable at the repo-root file --
    they belong in the per-instance ``foundry.toml``.  This guards the
    audience boundary structurally.
    """
    ref = InstanceRef(path=Path("instances/portfolio-assistant"))
    assert ref.path == Path("instances/portfolio-assistant")

    with pytest.raises(ValidationError, match="schema_prefix"):
        InstanceRef.model_validate(
            {
                "path": "instances/portfolio-assistant",
                "schema_prefix": "leaked",
            },
        )


def test_repo_config_empty_defaults() -> None:
    """A repo with no targets and no instances is still a valid model
    instance -- useful for bootstrap and for unit tests.  ``schema_version``
    defaults to :data:`CURRENT_SCHEMA_VERSION`.
    """
    repo = FoundryRepoConfig()
    assert repo.schema_version == CURRENT_SCHEMA_VERSION
    assert repo.targets == {}
    assert repo.instances == []


def test_repo_config_round_trip() -> None:
    """The example shape from the EPIC validates end-to-end.  Captures
    the contract surface the F-PR2 loader will produce.
    """
    repo = FoundryRepoConfig.model_validate(
        {
            "schema_version": CURRENT_SCHEMA_VERSION,
            "targets": {
                "dev": {
                    "host": "https://workspace.cloud.databricks.com",
                    "catalog": "main",
                    "profile": "dev",
                },
            },
            "instances": [{"path": "instances/portfolio-assistant"}],
        },
    )
    assert repo.targets["dev"].host == "https://workspace.cloud.databricks.com"
    assert repo.instances[0].path == Path("instances/portfolio-assistant")


def test_repo_config_rejects_per_instance_field_at_root() -> None:
    """Per-instance identity fields (e.g. ``schema_prefix``) cannot
    leak into the ``[[instances]]`` array entries at the repo root.
    """
    with pytest.raises(ValidationError):
        FoundryRepoConfig.model_validate(
            {
                "instances": [
                    {
                        "path": "instances/portfolio-assistant",
                        "schema_prefix": "wrong_file",
                    },
                ],
            },
        )


def test_repo_config_unique_catalogs_pass_silently(
    recwarn: pytest.WarningsRecorder,
) -> None:
    """Distinct catalogs across targets are the happy path -- no
    warning, no error.  Verifies the duplicate-catalog validator
    introduced for #210 (init wizard) does not fire spuriously on
    valid configs.
    """
    FoundryRepoConfig.model_validate(
        {
            "targets": {
                "dev": {"host": "https://wks.example.com", "catalog": "dev_cat"},
                "staging": {"host": "https://wks.example.com", "catalog": "staging_cat"},
                "prod": {"host": "https://wks.example.com", "catalog": "prod_cat"},
            },
        },
    )
    catalog_warnings = [w for w in recwarn.list if "catalog" in str(w.message)]
    assert catalog_warnings == []


def test_repo_config_warns_on_duplicate_catalogs() -> None:
    """When two targets share a catalog, the loader emits a
    ``UserWarning`` naming the duplicate and the targets sharing it.

    The validator stays at warning (not error) severity so pre-#210
    repos with overlapping catalogs continue to load; the strict
    enforcement happens in the ``agent-foundry init`` wizard.
    """
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        FoundryRepoConfig.model_validate(
            {
                "targets": {
                    "dev": {"host": "https://wks.example.com", "catalog": "shared"},
                    "staging": {"host": "https://wks.example.com", "catalog": "shared"},
                },
            },
        )

    catalog_warnings = [w for w in captured if issubclass(w.category, UserWarning) and "catalog" in str(w.message)]
    assert len(catalog_warnings) == 1
    message = str(catalog_warnings[0].message)
    assert "'shared'" in message
    assert "dev" in message
    assert "staging" in message


def test_repo_config_ignores_empty_catalogs_when_checking_duplicates() -> None:
    """Targets with empty / unset catalogs are partial configs (dry-run
    / config-show callers must still load them).  They should not
    trigger the duplicate-catalog warning -- the orchestrator handles
    "catalog required for real deploy" separately.
    """
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        FoundryRepoConfig.model_validate(
            {
                "targets": {
                    "dev": {"host": "https://wks.example.com"},
                    "staging": {"host": "https://wks.example.com"},
                },
            },
        )
    catalog_warnings = [w for w in captured if issubclass(w.category, UserWarning) and "catalog" in str(w.message)]
    assert catalog_warnings == []


# ---------------------------------------------------------------------------
# FoundryInstanceConfig
# ---------------------------------------------------------------------------


def test_instance_config_requires_identity_fields() -> None:
    """``name`` and ``schema_prefix`` have no defaults -- they are the
    minimum viable identity for any instance.  Forgetting them in the
    per-instance ``foundry.toml`` is a load-time error.

    Note: ``app_name`` and ``chat_ui_app_name`` are *not* required --
    they derive from ``name`` via the model_validator added in PR 2.
    """
    with pytest.raises(ValidationError):
        FoundryInstanceConfig.model_validate({"description": "missing identity"})


def test_instance_config_minimal_payload_validates() -> None:
    instance = FoundryInstanceConfig(
        name="portfolio-assistant",
        schema_prefix="portfolio_assistant",
    )
    assert instance.name == "portfolio-assistant"
    assert instance.description == ""


def test_instance_config_rejects_payload_toggles() -> None:
    """Feature toggles (chat history, file upload, ...) moved out of
    ``foundry.toml`` into the agent payload ``spec``
    (``payloads/agent/payload.yml``).  Authoring them at the instance
    level must now fail the ``extra="forbid"`` boundary so stale
    foundry.toml blocks surface loudly instead of silently no-op'ing.
    """
    for moved_key in ("chat_history", "file_upload"):
        with pytest.raises(ValidationError, match=moved_key):
            FoundryInstanceConfig.model_validate(
                {
                    "name": "x",
                    "schema_prefix": "x",
                    moved_key: {"enabled": True},
                },
            )


# ---------------------------------------------------------------------------
# Name-derived defaults (PR 2)
# ---------------------------------------------------------------------------
#
# ``app_name`` and ``chat_ui_app_name`` are no longer required on the
# per-instance ``foundry.toml`` -- omitting them derives clean defaults
# from ``name``.  Explicit values still win (escape hatch for the
# atypical instance whose app names diverge from the instance name).


def test_app_name_defaults_to_name() -> None:
    """The common case: a single-app instance has no reason to repeat
    ``app_name = name``.  Omitting it must produce the same wire
    contract as authoring it explicitly.
    """
    instance = FoundryInstanceConfig(
        name="portfolio-assistant",
        schema_prefix="portfolio_assistant",
    )
    assert instance.app_name == "portfolio-assistant"


def test_chat_ui_app_name_defaults_to_app_name_suffix() -> None:
    """``chat_ui_app_name`` derives from ``app_name`` (which itself
    derives from ``name`` when omitted).  Two-step derivation: omit
    both and you get ``<name>-ui``.
    """
    instance = FoundryInstanceConfig(
        name="portfolio-assistant",
        schema_prefix="portfolio_assistant",
    )
    assert instance.chat_ui_app_name == "portfolio-assistant-ui"


def test_explicit_app_name_is_preserved() -> None:
    """The escape hatch: authoring ``app_name`` explicitly must win
    over the derivation -- otherwise instances whose authored name
    is too long (>30 chars for Databricks Apps) couldn't override.
    """
    instance = FoundryInstanceConfig(
        name="portfolio-assistant",
        schema_prefix="portfolio_assistant",
        app_name="pa-short",
    )
    assert instance.app_name == "pa-short"
    # And the chat UI derivation chains off the explicit value:
    assert instance.chat_ui_app_name == "pa-short-ui"


def test_explicit_chat_ui_app_name_is_preserved() -> None:
    """Independent escape hatch for the chat UI name -- it doesn't
    have to follow the ``-ui`` convention.
    """
    instance = FoundryInstanceConfig(
        name="portfolio-assistant",
        schema_prefix="portfolio_assistant",
        chat_ui_app_name="pa-custom-frontend",
    )
    assert instance.app_name == "portfolio-assistant"
    assert instance.chat_ui_app_name == "pa-custom-frontend"


def test_both_explicit_app_names_preserved() -> None:
    """Authoring both explicitly is fully respected -- no surprise
    derivation overwrites either value.
    """
    instance = FoundryInstanceConfig(
        name="portfolio-assistant",
        schema_prefix="portfolio_assistant",
        app_name="pa-app",
        chat_ui_app_name="pa-ui-app",
    )
    assert instance.app_name == "pa-app"
    assert instance.chat_ui_app_name == "pa-ui-app"


def test_instance_config_full_payload_round_trip() -> None:
    instance = FoundryInstanceConfig.model_validate(
        {
            "name": "portfolio-assistant",
            "description": "Agent with Genie + Vector Search",
            "schema_prefix": "portfolio_assistant",
            "vs_endpoint_name": "vs_docs_demo",
        },
    )
    assert instance.vs_endpoint_name == "vs_docs_demo"


def test_instance_config_rejects_repo_level_keys() -> None:
    """The other half of the audience boundary: workspace-level keys
    (``host``, ``catalog``, ``profile``) cannot be authored inside a
    per-instance ``foundry.toml``.  ``warehouse_id`` is *not* in this
    list since ``schema_version = 2`` -- it belongs in ``[warehouse]``
    on the per-instance file now.
    """
    for repo_level_key in ("host", "catalog", "profile"):
        with pytest.raises(ValidationError, match=repo_level_key):
            FoundryInstanceConfig.model_validate(
                {
                    "name": "x",
                    "schema_prefix": "x",
                    repo_level_key: "leaked",
                },
            )


# ---------------------------------------------------------------------------
# Interpolation-token guard (F-PR8)
# ---------------------------------------------------------------------------
#
# ``foundry.toml`` fields are the *source* of the variable bag the
# framework substitutes into per-brick YAMLs via ``${var.*}``.  Letting
# the same tokens leak into the typed authoring layer would create a
# circular template language with no defined evaluation order; the
# validators below enforce that contract.


@pytest.mark.parametrize(
    "field_name,value",
    [
        ("host", "${var.workspace_host}"),
        ("catalog", "${var.catalog}"),
        ("profile", "${var.profile}"),
        ("deployer", "${var.deployer}"),
    ],
)
def test_target_rejects_var_interpolation(field_name: str, value: str) -> None:
    """``${var.*}`` is the most common interpolation form -- and the one
    we explicitly want to block on ``foundry.toml`` fields.
    """
    with pytest.raises(ValidationError, match=field_name):
        Target.model_validate({field_name: value})


def test_target_rejects_schema_var_via_alias() -> None:
    """The ``schema`` alias (Pydantic attribute ``schema_``) is also a
    typed field and must reject interpolation tokens through the same
    validator.
    """
    with pytest.raises(ValidationError, match="schema_"):
        Target.model_validate({"schema": "${var.schema}"})


def test_target_rejects_non_var_interpolation_tokens() -> None:
    """The guard is not specific to ``${var.*}`` -- any ``${...}`` form
    is rejected.  Future template prefixes (``${env.*}``, ``${secret.*}``,
    ...) are blocked by construction; if the framework ever adopts one,
    it has to be opted in deliberately rather than silently working.
    """
    with pytest.raises(ValidationError, match="host"):
        Target.model_validate({"host": "${env.DATABRICKS_HOST}"})


def test_target_accepts_literal_dollar_sign() -> None:
    """Regression guard: only the full ``${...}`` form is rejected.
    A bare ``$VAR`` (shell-style) or stray ``$`` must still validate --
    otherwise we'd reject perfectly valid passwords/paths.
    """
    target = Target.model_validate(
        {"host": "https://example.cloud.databricks.com/$tenant"},
    )
    assert target.host == "https://example.cloud.databricks.com/$tenant"


@pytest.mark.parametrize(
    "field_name,value",
    [
        ("name", "${var.app_name}-instance"),
        ("description", "Agent for ${var.team}"),
        ("schema_prefix", "${var.schema_prefix}"),
        ("app_name", "${var.app_name}"),
        ("chat_ui_app_name", "${var.app_name}-ui"),
        ("vs_endpoint_name", "${var.vs_endpoint}"),
    ],
)
def test_instance_config_rejects_var_interpolation(
    field_name: str,
    value: str,
) -> None:
    """Identity fields are what ``${var.*}`` resolves *to*; they cannot
    themselves be templates.
    """
    payload = {"name": "x", "schema_prefix": "x", field_name: value}
    with pytest.raises(ValidationError, match=field_name):
        FoundryInstanceConfig.model_validate(payload)


def test_interpolation_error_message_names_field_and_token() -> None:
    """Operator-facing error messages should identify both the field
    that failed *and* the specific token, so the fix is obvious without
    re-reading the schema.
    """
    with pytest.raises(ValidationError) as excinfo:
        Target.model_validate({"host": "${var.workspace_host}"})

    msg = str(excinfo.value)
    assert "host" in msg, "error message must name the failing field"
    assert "${var.workspace_host}" in msg, "error message must echo the offending token for quick diagnosis"


# ---------------------------------------------------------------------------
# Public-API exposure (smoke test for the re-export wiring)
# ---------------------------------------------------------------------------


def test_models_re_exported_from_top_level_package() -> None:
    """All the typed-config names must be reachable as
    ``agent_foundry.<Name>``.

    Keeps the public surface stable for downstreams that don't want to
    reach into ``agent_foundry.public.foundry_config``.
    """
    import agent_foundry

    for name in (
        "CURRENT_SCHEMA_VERSION",
        "FoundryInstanceConfig",
        "FoundryRepoConfig",
        "InstanceRef",
        "Target",
        "Warehouse",
    ):
        assert name in agent_foundry.__all__, f"{name} missing from agent_foundry.__all__"
        assert hasattr(agent_foundry, name), f"agent_foundry.{name} not importable"


# ---------------------------------------------------------------------------
# Warehouse (schema_version = 2)
# ---------------------------------------------------------------------------


def test_warehouse_defaults_to_dedicated_mode() -> None:
    """An unauthored ``[warehouse]`` section means "provision a new,
    dedicated warehouse" -- the most common SA workflow.
    """
    wh = Warehouse()
    assert wh.mode == "dedicated"
    assert wh.warehouse_id == ""


def test_warehouse_reuse_requires_warehouse_id() -> None:
    """``mode = "reuse"`` without ``warehouse_id`` is meaningless and
    must fail at load time, not silently bind to nothing.
    """
    with pytest.raises(ValidationError, match="warehouse_id"):
        Warehouse.model_validate({"mode": "reuse"})


def test_warehouse_dedicated_rejects_warehouse_id() -> None:
    """Typo guard: ``mode = "dedicated"`` with ``warehouse_id`` set
    looks like the author intended to reuse an existing warehouse but
    forgot to flip the mode toggle.  Fail loudly.
    """
    with pytest.raises(ValidationError, match="dedicated"):
        Warehouse.model_validate({"mode": "dedicated", "warehouse_id": "abc"})


def test_warehouse_reuse_with_id_round_trip() -> None:
    wh = Warehouse.model_validate({"mode": "reuse", "warehouse_id": "abc123"})
    assert wh.mode == "reuse"
    assert wh.warehouse_id == "abc123"


def test_warehouse_rejects_unknown_keys() -> None:
    """The same ``extra="forbid"`` audience boundary -- typos like
    ``size`` (which lives in ``warehouse/config.yml``) must surface
    instead of being silently dropped.
    """
    with pytest.raises(ValidationError, match="size"):
        Warehouse.model_validate({"mode": "dedicated", "size": "small"})


def test_warehouse_rejects_interpolation_token() -> None:
    """Same interpolation guard as the other typed fields (F-PR8)."""
    with pytest.raises(ValidationError, match="warehouse_id"):
        Warehouse.model_validate(
            {"mode": "reuse", "warehouse_id": "${var.warehouse_id}"},
        )


def test_warehouse_rejects_unknown_mode() -> None:
    """Two modes only.  Anything else is a typo and must fail."""
    with pytest.raises(ValidationError, match="mode"):
        Warehouse.model_validate({"mode": "shared"})


def test_instance_config_accepts_warehouse_block() -> None:
    """The new ``[warehouse]`` section is fully wired into
    :class:`FoundryInstanceConfig`.
    """
    instance = FoundryInstanceConfig.model_validate(
        {
            "name": "portfolio-assistant",
            "schema_prefix": "portfolio_assistant",
            "warehouse": {"mode": "reuse", "warehouse_id": "abc123"},
        },
    )
    assert instance.warehouse.mode == "reuse"
    assert instance.warehouse.warehouse_id == "abc123"


def test_instance_config_warehouse_defaults_to_dedicated() -> None:
    instance = FoundryInstanceConfig.model_validate(
        {"name": "x", "schema_prefix": "x"},
    )
    assert instance.warehouse.mode == "dedicated"
    assert instance.warehouse.warehouse_id == ""
