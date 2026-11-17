"""
Typed Configuration Models
===========================
Pydantic-based configuration that replaces the flat ``dict[str, str]``
variable bag with validated, typed models and computed derived fields.

The key design ideas:

- **Users declare inputs** (name, catalog, schema_prefix, llm_endpoint, ...).
- **Derived values are computed** (tracing_destination, agent_schema, ...).
- **State outputs are separate** (warehouse_id, genie_space_id, ...).
- **``.to_variables()``** bridges back to the legacy ``dict[str, str]``
  format so existing brick scripts work unchanged during incremental migration.

Usage::

    config = load_instance_config(repo_root, instance="my-app", target_name="dev")
    variables = config.to_variables()          # for legacy brick scripts
    variables = config.to_variables(state)     # include deployment outputs
"""

from __future__ import annotations

from importlib import resources as _resources
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from pydantic import BaseModel, ConfigDict, computed_field, model_validator

if TYPE_CHECKING:
    from agent_foundry.public import StateOutputs


def _load_builtin_eval_defaults() -> dict[str, Any]:
    """Load evaluation brick defaults from package data.

    Replaces the previous filesystem read at
    ``<project_root>/bricks/evaluation/conf/eval_config.yml`` (issue #81,
    68-A): the path-walking version broke as soon as ``agent_foundry``
    was installed as a wheel and there was no sibling ``bricks/`` dir
    on disk.
    """
    try:
        ref = _resources.files("bricks.evaluation").joinpath(
            "conf",
            "eval_config.yml",
        )
        text = ref.read_text(encoding="utf-8")
    except (ModuleNotFoundError, FileNotFoundError):
        return {}
    return yaml.safe_load(text) or {}


# ---------------------------------------------------------------------------
# Tag key -- single source of truth for all resource tagging
# ---------------------------------------------------------------------------

TAG_KEY_MANAGED_BY = "ai_solution_managed_by"


def merge_managed_by_tags(
    app_name: str | None,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """Merge caller tags, then pin ``ai_solution_managed_by`` so it cannot be overwritten.

    Skips the Foundry key when *app_name* is missing or blank (do not
    write an empty tag).
    """
    tags = {str(key): str(value) for key, value in dict(extra or {}).items()}
    name = (app_name or "").strip()
    if name:
        tags[TAG_KEY_MANAGED_BY] = name
    return tags


# ---------------------------------------------------------------------------
# State output aggregation
# ---------------------------------------------------------------------------
#
# 68-C (issue #84) replaced the central ``STATE_KEY_MAPS`` dict with a
# registry walk: each ``Brick`` declares its own ``load_state_outputs``
# so adding a new brick no longer requires editing this module.


def load_state_outputs(
    instance_dir: Path,
    target: str,
    ws: Any | None = None,
) -> StateOutputs:
    """Aggregate every registered brick's state outputs into a typed
    :class:`~agent_foundry.public.StateOutputs` wrapper.

    Kept as a top-level helper (exported from ``agent_foundry.__all__``)
    so downstream consumers can refresh variables without constructing
    a :class:`~agent_foundry.bricks.DeployContext` by hand.

    Issue #159 replaced the legacy ``dict[str, str]`` return shape with
    :class:`StateOutputs`.  Callers that previously did
    ``variables.update(load_state_outputs(...))`` should now do
    ``variables.update(load_state_outputs(...).variables)`` -- the
    payload lives on the :attr:`StateOutputs.variables` field, not on
    the model itself, because Pydantic's natural iter protocol would
    otherwise yield the model's fields rather than the inner var
    key/value pairs.
    """
    from agent_foundry.bricks import DeployContext, iter_bricks
    from agent_foundry.public import StateOutputs
    from agent_foundry.state import BrickStateValidationError

    variables: dict[str, str] = {}
    bricks_loaded: list[str] = []
    for brick in iter_bricks():
        ctx = DeployContext(
            instance_dir=instance_dir,
            target=target,
            ws=ws,
        )
        try:
            outputs = brick.load_state_outputs(ctx)
        except BrickStateValidationError:
            # #156: schema violations are a contract failure, not a
            # transient I/O hiccup.  Propagate so the deploy halts
            # before a downstream brick consumes invalid state.
            raise
        except Exception as exc:
            print(f"  WARNING: load_state_outputs failed for brick {brick.name!r}: {exc}")
            continue
        if outputs:
            bricks_loaded.append(brick.name)
        variables.update(outputs)
    return StateOutputs(variables=variables, bricks_loaded=bricks_loaded)


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------


class TargetConfig(BaseModel):
    """Environment-specific settings from ``[targets.<target>]`` in ``foundry.toml``."""

    # Single source of truth for class-level config -- mypy mypy[misc]
    # otherwise flags the ``frozen=True`` class kwarg as ambiguous when an
    # explicit ``model_config`` is also present.  Issue #228.
    model_config = ConfigDict(frozen=True, populate_by_name=True)

    host: str = ""
    catalog: str = ""
    schema_: str = "analytics"
    warehouse_id: str = ""
    profile: str = ""
    deployer: str = ""


class WarehouseConfig(BaseModel, frozen=True):
    """Dedicated SQL warehouse sizing from ``warehouse/config.yml``."""

    size: str = "2X-Small"
    auto_stop_mins: int = 10
    tags: dict[str, str] = {}
    # Multi-cluster autoscaling bounds. The REST contract requires
    # 1 <= min <= max <= 40; we keep the conservative single-cluster
    # default so existing instances are unaffected. Scaffolds and
    # examples surface non-default values explicitly.
    min_num_clusters: int = 1
    max_num_clusters: int = 1

    @model_validator(mode="after")
    def _validate_cluster_bounds(self) -> "WarehouseConfig":
        if not 1 <= self.min_num_clusters:
            raise ValueError(f"min_num_clusters must be >= 1, got {self.min_num_clusters}")
        if self.max_num_clusters < self.min_num_clusters:
            raise ValueError(
                f"max_num_clusters ({self.max_num_clusters}) must be >= min_num_clusters ({self.min_num_clusters})"
            )
        if self.max_num_clusters > 40:
            raise ValueError(f"max_num_clusters must be <= 40, got {self.max_num_clusters}")
        return self


class ScorerSpec(BaseModel):
    """A scorer entry with constructor arguments, e.g. a ``Guidelines`` judge."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    args: dict[str, Any] = {}


class EvaluationConfig(BaseModel, frozen=True):
    """Evaluation and monitoring from ``evaluation/eval_config.yml``."""

    scorers: dict[str, list[str | ScorerSpec]] = {}
    max_traces: int = 100
    production_monitoring: dict[str, Any] = {}


class DeploymentState(BaseModel):
    """Mutable state populated as bricks deploy."""

    warehouse_id: str | None = None
    genie_space_id: str | None = None
    experiment_id: str | None = None
    vs_index_name: str | None = None
    vs_endpoint_name: str | None = None


# ---------------------------------------------------------------------------
# Root config model
# ---------------------------------------------------------------------------


class InstanceConfig(BaseModel, frozen=True):
    """Fully resolved, validated configuration for one application instance."""

    name: str
    description: str = ""
    target_name: str = "dev"
    schema_prefix: str
    vs_endpoint_name: str = ""
    app_name: str = ""
    chat_ui_app_name: str = ""
    # Set by the loader from the payload adapter; not authored in foundry.toml.
    app_resource_name_override: str = ""

    target: TargetConfig = TargetConfig()
    warehouse: WarehouseConfig = WarehouseConfig()
    evaluation: EvaluationConfig = EvaluationConfig()

    @model_validator(mode="after")
    def _default_app_name(self) -> "InstanceConfig":
        if not self.app_name:
            object.__setattr__(self, "app_name", self.name)
        return self

    # --- Naming helpers ---

    def _display_name(self, suffix: str) -> str:
        """Tier 1: ``[target] app-name-suffix`` for display-name resources."""
        return f"[{self.target_name}] {self.app_name}-{suffix}"

    def _resource_name(self, suffix: str) -> str:
        """Tier 2: ``target-app-name-suffix`` for API resource names."""
        return f"{self.target_name}-{self.app_name}-{suffix}"

    # --- Computed fields (derived, not user-editable) ---
    #
    # The ``@computed_field`` + ``@property`` stack below is the
    # idiomatic Pydantic v2 pattern (see Pydantic docs >> "Computed
    # fields") and works correctly at runtime.  Mypy 1.20+ raises
    # ``[prop-decorator]`` on any decorator stacked on ``@property``,
    # even with the pydantic mypy plugin enabled.  Each line below
    # carries a narrow ignore so a *new* ``prop-decorator`` issue
    # outside of computed_field still surfaces.  Issue #228; tracked
    # upstream as pydantic/pydantic#11160.

    @computed_field  # type: ignore[prop-decorator]
    @property
    def agent_schema(self) -> str:
        """UC schema for agent artifacts: ``<catalog>.<schema_prefix>_agent``."""
        return f"{self.target.catalog}.{self.schema_prefix}_agent"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def agent_schema_name(self) -> str:
        """The agent schema without its catalog: ``<schema_prefix>_agent``.

        ``agent_schema`` is two-part, so it cannot be back-quoted as a unit --
        `` `cat.sch_agent` `` names one schema called "cat.sch_agent".  SQL
        templates that address the agent schema in an identifier position
        compose `` `${var.catalog}`.`${var.agent_schema_name}` `` instead, which
        survives a hyphen in either part.  ``agent_schema`` stays as-is for API
        fields (UC function names in ``tools.yml``), which must not be quoted.
        """
        return f"{self.schema_prefix}_agent"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def tracing_destination(self) -> str:
        """UC location for OTEL trace tables."""
        return f"{self.target.catalog}.{self.schema_prefix}_agent"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def solution_root(self) -> str:
        """Shared workspace root, target-partitioned for multi-target isolation."""
        return f"/Shared/agent-foundry/{self.target_name}/{self.app_name}"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def tracing_experiment(self) -> str:
        """MLflow experiment path for traces, unique per instance."""
        return f"{self.solution_root}/experiments/{self._resource_name('traces')}"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def tracing_table_prefix(self) -> str:
        """Fixed prefix for MLflow UC table names."""
        return "mlflow_experiment"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def tracing_uc_destination(self) -> str:
        """Full 3-part UC destination for app.yaml: ``<dest>.<prefix>``."""
        return f"{self.tracing_destination}.{self.tracing_table_prefix}"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def tracing_schema(self) -> str:
        """Schema-only part of the tracing destination (for dashboard queries)."""
        return f"{self.schema_prefix}_agent"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def warehouse_display_name(self) -> str:
        """Display name for the dedicated SQL warehouse."""
        return self._display_name("warehouse")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def dashboard_display_name(self) -> str:
        """Display name for the observability dashboard."""
        return self._display_name("dashboard")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def genie_display_prefix(self) -> str:
        """Prefix for Genie Space titles: ``[target] app-name``."""
        return f"[{self.target_name}] {self.app_name}"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def app_resource_name(self) -> str:
        """Databricks App resource name (no brackets)."""
        return self.app_resource_name_override or self._resource_name("app")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def chat_ui_resource_name(self) -> str:
        """Chat UI App resource name (no brackets)."""
        return self._resource_name("app-ui")

    # --- Bridge to legacy dict[str, str] ---

    def to_variables(self, state: DeploymentState | None = None) -> dict[str, str]:
        """Flatten to the legacy ``dict[str, str]`` format.

        Existing brick scripts receive this dict and continue to work
        unchanged. Computed fields are pre-resolved -- no ``${var.*}``
        substitution needed for derived values.
        """
        v: dict[str, str] = {
            "host": self.target.host,
            "catalog": self.target.catalog,
            "schema": self.target.schema_,
            "warehouse_id": self.target.warehouse_id,
            "deployer": self.target.deployer,
            "profile": self.target.profile,
            "target_name": self.target_name,
            "app_name": self.app_name,
            "schema_prefix": self.schema_prefix,
            "vs_endpoint_name": self.vs_endpoint_name,
            "chat_ui_app_name": self.chat_ui_resource_name,
            # Computed (single source of truth)
            "solution_root": self.solution_root,
            "workspace_root": self.solution_root,
            "agent_schema": self.agent_schema,
            "agent_schema_name": self.agent_schema_name,
            "tracing_destination": self.tracing_destination,
            "tracing_experiment": self.tracing_experiment,
            "tracing_table_prefix": self.tracing_table_prefix,
            # Naming convention: [target] solution-name-brick-name
            "warehouse_display_name": self.warehouse_display_name,
            "dashboard_display_name": self.dashboard_display_name,
            "genie_display_prefix": self.genie_display_prefix,
            "app_resource_name": self.app_resource_name,
            "chat_ui_resource_name": self.chat_ui_resource_name,
        }

        if state:
            if state.warehouse_id:
                v["warehouse_id"] = state.warehouse_id
            if state.genie_space_id:
                v["genie_space_id"] = state.genie_space_id
            if state.experiment_id:
                v["experiment_id"] = state.experiment_id
            if state.vs_index_name:
                v["vs_index_name"] = state.vs_index_name
            if state.vs_endpoint_name:
                v["vs_endpoint_name"] = state.vs_endpoint_name

        return v


# ---------------------------------------------------------------------------
# YAML helpers
# ---------------------------------------------------------------------------


def load_yaml(path: Path) -> dict[str, Any]:
    """Load a YAML file, returning an empty dict if the file doesn't exist."""
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def parse_yaml_file(path: Path) -> dict[str, Any]:
    """Load a YAML file, returning empty dict if file is missing or empty.

    Unlike :func:`load_yaml`, the text is stripped before parsing, so a
    trailing ``|`` block scalar loses its final newline.  Genie benchmark
    SQL and skill manifests depend on that; keep the two separate.
    """
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return {}
    return yaml.safe_load(text) or {}


def extract_warehouse_config(raw: dict[str, Any]) -> dict[str, Any]:
    """Map warehouse/config.yml keys to WarehouseConfig field names."""
    result: dict[str, Any] = {}
    for key in ("size", "auto_stop_mins", "tags", "min_num_clusters", "max_num_clusters"):
        if key in raw:
            result[key] = raw[key]
    return result


def extract_evaluation_config(
    defaults: dict[str, Any],
    instance: dict[str, Any],
) -> dict[str, Any]:
    """Merge brick-level eval defaults with instance overrides."""
    merged = dict(defaults)
    merged.update(instance)
    result: dict[str, Any] = {}
    if "scorers" in merged:
        result["scorers"] = merged["scorers"]
    if "max_traces" in merged:
        result["max_traces"] = merged["max_traces"]
    if "production_monitoring" in merged:
        result["production_monitoring"] = merged["production_monitoring"]
    return result
