"""Typed authoring contract for downstream ``foundry.toml`` files.

Issue #191 (typed foundry config contract): each downstream repo
authors two TOML files:

* Repo-root ``foundry.toml`` -- typed via :class:`FoundryRepoConfig`.
  Owns workspace targets and the list of instances in the repo.
* Per-instance ``instances/<name>/foundry.toml`` -- typed via
  :class:`FoundryInstanceConfig`.  Owns the identity / feature toggle
  fields.

The split encodes an audience boundary: operators / deployers own the
repo-root file; agent authors own the per-instance file.  Each file's
Pydantic model uses ``extra="forbid"`` so that fields cannot leak
across the boundary by accident (a typo in the wrong file fails at
load time rather than being silently ignored).

This module ships the models only; :mod:`agent_foundry.loader` reads
them off disk.

Mapping to the existing runtime models
--------------------------------------

These models describe the *on-disk authoring shape*.  They are
deliberately distinct from the runtime models in
:mod:`agent_foundry.config`:

* :class:`Target` mirrors :class:`agent_foundry.config.TargetConfig`
  but is mutable and uses ``extra="forbid"``.
* :class:`FoundryInstanceConfig` mirrors the fields of
  :class:`agent_foundry.config.InstanceConfig` that are user-authored.
  Computed fields (``agent_schema``, ``tracing_destination``, ...)
  remain on the runtime model and are derived during composition --
  not authored.

The loader composes these authoring models with the agent payload
``spec`` (:class:`agent_foundry.contracts.agent.AgentPayloadSpec`) and
the per-brick YAML contracts
(:class:`agent_foundry.config.WarehouseConfig`,
:class:`agent_foundry.config.EvaluationConfig`) to produce the runtime
:class:`~agent_foundry.config.InstanceConfig` the rest of the
framework consumes.

Interpolation guard (F-PR8)
---------------------------

Every string field on these models rejects ``${...}`` interpolation
tokens at validation time.  The typed ``foundry.toml`` layer is the
*source* of the variable bag that the rest of the framework
substitutes into per-brick YAMLs via ``${var.*}``; allowing the same
tokens here would create a circular template language with no defined
evaluation order.  See :func:`_reject_interpolation_token` below for
the implementation and per-field wiring on :class:`Target`,
:class:`FoundryInstanceConfig`, and :class:`ChatHistory`.
"""

from __future__ import annotations

import re
import warnings
from pathlib import Path
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

# ---------------------------------------------------------------------------
# Interpolation-token guard (issue #191, F-PR8)
# ---------------------------------------------------------------------------
#
# ``foundry.toml`` is the *source* of values that the rest of the framework
# substitutes into per-brick YAMLs via ``${var.*}``.  Allowing the same
# tokens *inside* foundry.toml fields would create a circular template
# language with no defined evaluation order ("does ``${var.app_name}``
# resolve before ``app_name`` itself?").
#
# The contract is therefore: any field validated by this module must be
# a literal value.  Per-brick YAMLs (``payloads/agent/payload.yml``,
# ``warehouse/config.yml``, ``evaluation/eval_config.yml``, ...) keep
# their templating -- only the typed authoring layer is locked down.

_INTERPOLATION_RE = re.compile(r"\$\{[^}]+\}")


def _reject_interpolation_token(
    value: str | None,
    info: ValidationInfo,
) -> str | None:
    """Reject any ``${...}`` token in a typed ``foundry.toml`` field.

    Returns *value* unchanged on success so it can be wired in as a
    Pydantic ``mode="after"`` field validator.  ``None`` passes through
    untouched (covers optional fields like ``ChatHistory.project_name``).
    """
    if value is None:
        return value
    match = _INTERPOLATION_RE.search(value)
    if match is None:
        return value
    raise ValueError(
        f"{info.field_name}: {value!r} contains interpolation token "
        f"{match.group(0)!r} -- foundry.toml fields must be literal "
        f"values, not interpolation tokens. Use per-brick YAMLs "
        f"(agent/, warehouse/, evaluation/, ...) for ${{var.*}} content."
    )


# ---------------------------------------------------------------------------
# Repo-root sub-models
# ---------------------------------------------------------------------------


class Target(BaseModel):
    """One environment target authored at the repo root.

    Mirrors the field set of
    :class:`agent_foundry.config.TargetConfig` but uses
    ``extra="forbid"`` so unknown keys in ``foundry.toml`` raise at
    load time rather than being silently dropped.

    All fields are optional in isolation, but the framework's deploy
    path requires at minimum ``host`` and ``catalog`` to be set for
    any target that actually receives a deploy.  That runtime
    requirement is enforced by the orchestrator (F-PR5), not here,
    because dry-run validation and ``agent-foundry config show`` must
    still be able to load a partially-configured target without
    failing.

    ``warehouse_id`` is **not** authored here -- it is a per-instance
    concern as of ``schema_version = 2`` (see :class:`Warehouse`).
    The repo-root target owns workspace identity (host, catalog,
    profile) and authoring identity (deployer); the warehouse a given
    instance binds to lives in
    :attr:`FoundryInstanceConfig.warehouse`.
    """

    model_config = ConfigDict(extra="forbid")

    host: str = ""
    catalog: str = ""
    # ``schema`` is a Pydantic ``BaseModel`` attribute; alias the user-
    # facing name to ``schema_`` (mirrors ``TargetConfig.schema_``).
    schema_: str = Field(default="analytics", alias="schema")
    profile: str = ""
    deployer: str = ""

    # Reject ``${var.*}`` (and any ``${...}``) in every authored field.
    # Targets are the *source* of the variable bag the rest of the
    # framework substitutes; self-referential templating here is a
    # contract violation.
    _no_interp = field_validator(
        "host",
        "catalog",
        "schema_",
        "profile",
        "deployer",
        mode="after",
    )(_reject_interpolation_token)


class InstanceRef(BaseModel):
    """A pointer from the repo-root file to one per-instance directory.

    The repo-root ``foundry.toml`` lists ``[[instances]]`` entries with
    only a ``path``; identity fields (``name``, ``schema_prefix``, ...)
    live in the per-instance ``foundry.toml`` at that path.  Keeping
    the repo-root entry minimal ensures the audience split is
    structural -- you cannot author an instance identity field at the
    repo root and have it "accidentally work".
    """

    model_config = ConfigDict(extra="forbid")

    path: Path


CURRENT_SCHEMA_VERSION: int = 2
"""The only ``schema_version`` the loader accepts.

Bumped from 1 -> 2 when ``warehouse_id`` moved from
:class:`Target` (repo-root) to :class:`Warehouse` (per-instance).
Older repos must edit ``foundry.toml`` to match the new layout
before the loader will read them.  See the CHANGELOG migration
guide for the step-by-step.
"""


class FoundryRepoConfig(BaseModel):
    """Typed shape of the repo-root ``foundry.toml``.

    Example::

        schema_version = 2

        [targets.dev]
        host    = "https://workspace.cloud.databricks.com"
        catalog = "main"
        profile = "dev-profile"

        [[instances]]
        path = "instances/portfolio-assistant"

    The ``schema_version`` field pins the framework <-> downstream
    contract so future breaking changes to the on-disk shape are
    explicit -- the loader refuses to read files whose
    ``schema_version`` it does not recognise (see
    :data:`CURRENT_SCHEMA_VERSION`).
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int = CURRENT_SCHEMA_VERSION
    targets: dict[str, Target] = Field(default_factory=dict)
    instances: list[InstanceRef] = Field(default_factory=list)

    @model_validator(mode="after")
    def _warn_on_duplicate_catalogs(self) -> "FoundryRepoConfig":
        """Warn when two targets share the same catalog.

        Hosts may legitimately repeat across ``[targets.*]`` blocks
        (same workspace serving dev/staging/prod with isolation
        provided by Unity Catalog), but catalogs must differ -- a
        shared catalog across targets defeats the isolation guarantee
        and causes silent cross-environment writes.

        We emit a warning rather than raising because pre-#210 repos
        may already exist with overlapping catalogs and we do not
        want to break their loads on framework upgrade.  The
        ``agent-foundry init`` wizard enforces uniqueness strictly at
        authoring time; this validator is the runtime safety net for
        hand-edits.
        """
        # Skip targets that haven't been configured with a catalog at
        # all -- the orchestrator (F-PR5) is what enforces "host +
        # catalog required for a real deploy".  Dry-run / config-show
        # callers must still be able to load a partially-configured
        # repo without spurious warnings.
        seen: dict[str, list[str]] = {}
        for name, target in self.targets.items():
            if not target.catalog:
                continue
            seen.setdefault(target.catalog, []).append(name)

        duplicates = {catalog: target_names for catalog, target_names in seen.items() if len(target_names) > 1}
        if duplicates:
            details = ", ".join(
                f"{catalog!r} shared by [{', '.join(target_names)}]"
                for catalog, target_names in sorted(duplicates.items())
            )
            warnings.warn(
                "foundry.toml: catalogs should be unique across targets "
                "(same host is fine, same catalog defeats environment "
                f"isolation): {details}. Edit [targets.*].catalog to "
                "use distinct catalogs per environment.",
                UserWarning,
                stacklevel=2,
            )
        return self


# ---------------------------------------------------------------------------
# Per-instance model
# ---------------------------------------------------------------------------


class Warehouse(BaseModel):
    """Per-instance SQL warehouse binding.

    Introduced in ``schema_version = 2``: warehouse ownership moved
    from the repo-root :class:`Target` to the per-instance
    :class:`FoundryInstanceConfig`.  This matches the operational
    reality that each agent instance either needs its *own* dedicated
    warehouse (for cost attribution and lifecycle isolation) or
    explicitly opts to share an existing one with another workload.

    Two modes:

    * ``mode = "dedicated"`` (default) -- the warehouse brick
      provisions a new SQL warehouse for this instance.  Sizing,
      auto-stop, and tags come from
      ``instances/<name>/warehouse/config.yml``.  Authoring
      ``warehouse_id`` here is rejected (typo guard: the field is
      meaningless in dedicated mode).
    * ``mode = "reuse"`` -- the instance binds to an existing
      warehouse.  ``warehouse_id`` is required and the warehouse
      brick refuses to run (a stale ``warehouse/config.yml`` is
      rejected by the loader as a strict-overlap error to keep the
      authoring surface unambiguous).
    """

    model_config = ConfigDict(extra="forbid")

    mode: Literal["dedicated", "reuse"] = "dedicated"
    warehouse_id: str = ""

    _no_interp = field_validator("warehouse_id", mode="after")(
        _reject_interpolation_token,
    )

    @model_validator(mode="after")
    def _check_mode_id_consistency(self) -> "Warehouse":
        if self.mode == "reuse" and not self.warehouse_id:
            raise ValueError(
                '[warehouse] mode = "reuse" requires warehouse_id to be set '
                "to the existing workspace warehouse's id. Either set "
                'warehouse_id, or switch to mode = "dedicated" and let the '
                "warehouse brick provision a new one."
            )
        if self.mode == "dedicated" and self.warehouse_id:
            raise ValueError(
                '[warehouse] mode = "dedicated" cannot set warehouse_id '
                f"(got {self.warehouse_id!r}) -- the warehouse brick "
                "provisions the warehouse and writes its id to the "
                "warehouse/_state file at deploy time. Either drop "
                'warehouse_id, or switch to mode = "reuse" to bind to an '
                "existing warehouse."
            )
        return self


class FoundryInstanceConfig(BaseModel):
    """Typed shape of ``instances/<name>/foundry.toml``.

    Example::

        name             = "portfolio-assistant"
        description      = "Agent with Genie + Vector Search"
        schema_prefix    = "portfolio_assistant"
        vs_endpoint_name = "vs_docs_demo"

    Owns instance *identity* + the warehouse binding.  Runtime config
    and feature toggles (chat history, file upload, ...) are NOT authored
    here -- they live in the agent payload ``spec``
    (``payloads/agent/payload.yml``), the single per-payload config
    contract.

    Field semantics match the user-authored subset of
    :class:`agent_foundry.config.InstanceConfig`.  Computed fields
    (``agent_schema``, ``tracing_destination``, ``solution_root``, ...)
    are not on this model -- they are derived at composition time by
    the loader (F-PR2) and exposed via the runtime
    :class:`~agent_foundry.config.InstanceConfig`.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    schema_prefix: str
    app_name: str = ""
    chat_ui_app_name: str = ""
    vs_endpoint_name: str = ""
    warehouse: Warehouse = Field(default_factory=Warehouse)

    # Identity fields are the authored definitions ``${var.*}`` resolves
    # *to* downstream -- they cannot themselves be templates.
    _no_interp = field_validator(
        "name",
        "description",
        "schema_prefix",
        "app_name",
        "chat_ui_app_name",
        "vs_endpoint_name",
        mode="after",
    )(_reject_interpolation_token)

    @model_validator(mode="after")
    def _derive_app_names_from_name(self) -> "FoundryInstanceConfig":
        """Default ``app_name`` to ``name`` and ``chat_ui_app_name`` to
        ``<app_name>-ui`` when omitted.

        Removes the most common redundancy in per-instance
        ``foundry.toml``: for the typical single-app instance, the
        bare ``name`` carries enough information to derive the other
        two identity strings.  Explicit authored values still win,
        which preserves the escape hatch for instances whose app
        names diverge from the instance name (longer than the 30-char
        Databricks Apps limit, multi-app instances, ...).

        Mirrors the runtime
        :meth:`agent_foundry.config.InstanceConfig._default_app_name`
        pattern.  Lives on the authoring model so
        ``agent-foundry config show`` (and the JSON Schema-driven IDE
        view) surface the resolved values consistently.
        """
        if not self.app_name:
            object.__setattr__(self, "app_name", self.name)
        if not self.chat_ui_app_name:
            object.__setattr__(self, "chat_ui_app_name", f"{self.app_name}-ui")
        return self


__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "FoundryInstanceConfig",
    "FoundryRepoConfig",
    "InstanceRef",
    "Target",
    "Warehouse",
]
