"""
Pluggable brick registry (issue #82, 68-B)
===========================================
Defines the contract that every deployable brick implements, plus a
hybrid registry that ships built-ins as a static tuple *and* discovers
third-party bricks via Python entry points.

Also hosts :func:`load_brick_variables` -- the shared helper that
brick standalone-CLI entrypoints use to load the ``${var.*}`` bag
from ``foundry.toml``.

The goal of 68-B is to land the contract and prove it with one
migration (``sql_function``).  68-C migrates the remaining bricks and
deletes the legacy ``BRICK_DEPLOY_ORDER`` tuple + ``STATE_KEY_MAPS``.

Why a hybrid registry (not pure entry points)?

- Built-ins always ship with ``agent-foundry``.  Paying the
  ``importlib.metadata`` scan cost on every CLI invocation is wasted
  work for the 90% case.
- Deterministic ordering is easier to reason about when it is a static
  tuple in source than a set scraped from installed distributions.
- Third parties still plug in without forking: declare an entry point
  in the ``agent_foundry.bricks`` group and the framework picks it up.

Module name vs package ``bricks/``:

``agent_foundry.bricks`` is a flat module, not a subpackage, to avoid
collision with the existing top-level ``bricks/`` tree during the
registry rollout.  We may revisit the name in a future SemVer bump.
"""

from __future__ import annotations

import warnings
from importlib import metadata as _metadata
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Iterator, Literal, Protocol, cast, runtime_checkable

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    pass


# Entry-point group name third-party distributions declare under to
# register custom bricks.  Example (in a downstream pyproject.toml)::
#
#     [project.entry-points."agent_foundry.bricks"]
#     my_brick = "my_package.bricks:MyCustomBrick"
ENTRY_POINT_GROUP = "agent_foundry.bricks"


BrickStatus = Literal[
    "ok",
    "skipped",
    "deploy_error",
    "validate_error",
    "assemble_error",
    "destroy_error",
]


class DestroyPlanItem(BaseModel):
    """One resource a brick would remove during ``destroy``."""

    model_config = ConfigDict(extra="forbid")

    brick: str
    sub_instance: str | None = None
    resource: str
    destructive: bool = False
    reason: str | None = None


class DestroyPlan(BaseModel):
    """Aggregated destroy intent returned by :meth:`Brick.plan_destroy`."""

    model_config = ConfigDict(extra="forbid")

    items: list[DestroyPlanItem] = []


ResourceKind = Literal[
    "app",
    "dashboard",
    "genie_space",
    "job",
    "lakebase_project",
    "mcp_service",
    "mlflow_experiment",
    "registered_model",
    "serving_endpoint",
    "skill",
    "sql_warehouse",
    "uc_connection",
    "uc_function",
    "uc_schema",
    "vector_index",
]


class ResourceRef(BaseModel):
    """A workspace resource a brick's ``destroy`` deleted.

    The orchestrator waits until each reported resource is gone (see
    :mod:`agent_foundry.deploy.gone`) before it reports the destroy as ok.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: ResourceKind
    resource_id: str


class BrickResult(BaseModel):
    """Structured result of one brick's deploy step.

    One ``Brick.deploy(...)`` call may return multiple results when the
    brick has sub-instances (e.g. one ``sql_function`` brick deploys N
    function groups; each is its own ``BrickResult``).

    ``deleted`` is set by ``destroy`` only, and only for resources it
    actually deleted (not on skip, dry-run, or missing state).
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    status: BrickStatus
    rc: int = 0
    details: dict[str, Any] = {}
    deleted: list[ResourceRef] = []


class ScaffoldResult(BaseModel):
    """Outcome of one ``brick.scaffold(...)`` call.

    Each scaffolded brick lists the files it wrote, the files it would
    have written but skipped (e.g. existing files when ``--force`` was
    not passed), and an optional human-readable note (used by bricks
    without an authored config surface to redirect the user to the
    right place).
    """

    model_config = ConfigDict(extra="forbid")

    written: list[Path] = []
    skipped: list[Path] = []
    notes: list[str] = []


class DeployContext(BaseModel):
    """Everything a brick needs at deploy time.

    Kept deliberately small in 68-B.  Rich fields like a typed config
    view arrive in 68-C as individual bricks get migrated and actually
    need them.  Do not add optional kwargs here without a concrete
    caller requirement -- the goal is a stable public contract.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    instance_dir: Path
    target: str
    variables: dict[str, str] = {}
    dry_run: bool = False
    # ``ws`` and ``config`` are populated by the orchestrator when
    # available. Operator ``--dry-run`` may still pass a live client so
    # workspace-only state (Genie, MCP) can be read. Unit tests and
    # ``--offline`` leave ``ws`` None. Typed as ``Any`` to keep the core
    # model import-light; runtime callers should treat them as the real
    # WorkspaceClient / InstanceConfig. Deploy/destroy functions use
    # ``ctx.ws`` as-is and must not construct a client when it is None.
    # Standalone ``main()`` may call the factory once and pass ``ws=``.
    ws: Any | None = None
    config: Any | None = None


class DestroyContext(DeployContext):
    """Deploy context plus the safety knob for destructive teardown."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    force: bool = False


def deleted_refs(ctx: DestroyContext, rc: int, kind: ResourceKind, *resource_ids: str | None) -> list[ResourceRef]:
    """Refs for a delete that really ran and succeeded; empty on dry-run or failure."""
    if rc != 0 or ctx.dry_run:
        return []
    return [ResourceRef(kind=kind, resource_id=resource_id) for resource_id in resource_ids if resource_id]


@runtime_checkable
class Brick(Protocol):
    """Protocol every registered brick implements.

    Attributes
    ----------
    name
        Stable identifier used for logging, filtering (``--skip``), and
        deduplication when third parties register a brick with the same
        name as a built-in.  Must be unique across all registered bricks.
    deploy_order
        Integer-valued ordering key.  Built-ins use multiples of 10 so
        third-party bricks can slot between them (e.g. ``25`` to run
        after ``20`` and before ``30``).  Ties broken by ``name``.
    state_schema
        Optional Pydantic model describing the brick's state file shape.
        None for bricks that do not persist state (e.g. ``sql_function``).
        Currently informational; will be enforced in a future release.
    display_name
        Human-readable label used in CLI output (``--- <display_name> ---``).
        Preserves the historical capitalisation of ``BRICK_DEPLOY_ORDER``
        entries (e.g. ``"SQL Warehouse"`` not ``"warehouse"``).
    """

    name: str
    deploy_order: int
    state_schema: type[BaseModel] | None
    display_name: str

    def applies_to(self, ctx: DeployContext) -> bool:
        """Return True if this brick should run for *ctx*.

        Typical implementation: check whether the instance has the
        required sub-directory (e.g. ``sql_function`` checks for
        ``<instance_dir>/sql_functions/``).  The orchestrator skips
        bricks that return False and marks them ``skipped``.
        """
        ...

    def deploy(self, ctx: DeployContext) -> list[BrickResult]:
        """Deploy this brick.  Returns one result per sub-instance."""
        ...

    def load_state_outputs(self, ctx: DeployContext) -> dict[str, str]:
        """Return deploy-time outputs this brick wants exposed as variables.

        Called by the orchestrator both before deploy (to surface state
        from prior runs) and after (so downstream bricks in the same
        run see fresh outputs).  Variable names must be unique across
        all bricks; convention is ``<base>`` for flat-layout bricks
        (single sub-instance) and ``<base>__<sub_instance>`` for nested
        layouts.

        Return an empty dict when the brick has no outputs to publish
        (e.g. ``sql_function``).  Implementations should tolerate a
        missing state directory silently.
        """
        ...

    def plan_destroy(self, ctx: DestroyContext) -> DestroyPlan:
        """Return what this brick would destroy for *ctx*.

        The orchestrator aggregates these items across all registered
        bricks (without gating on :meth:`applies_to`) so operators see
        the full teardown plan before any API calls run.
        """
        ...

    def destroy(self, ctx: DestroyContext) -> list[BrickResult]:
        """Tear down resources previously created by :meth:`deploy`.

        Unlike :meth:`applies_to`, which checks on-disk config, destroy
        implementations should consult persisted workspace state to
        decide whether work remains.  Return
        ``status="skipped"`` when no state is found (idempotent).
        """
        ...


# ---------------------------------------------------------------------------
# Default destroy implementations (backwards-compatible for third parties)
# ---------------------------------------------------------------------------


class BrickDestroyDefaults:
    """Mixin providing no-op destroy methods for bricks not yet migrated."""

    def plan_destroy(self, ctx: DestroyContext) -> DestroyPlan:  # noqa: ARG002
        return DestroyPlan()

    def destroy(self, ctx: DestroyContext) -> list[BrickResult]:  # noqa: ARG002
        brick_name = getattr(self, "name", type(self).__name__)
        return [
            BrickResult(
                name=brick_name,
                status="skipped",
                rc=0,
                details={"reason": "brick does not implement destroy"},
            )
        ]


# ---------------------------------------------------------------------------
# Convenience base class for bricks that follow the standard layouts
# ---------------------------------------------------------------------------


class StandardLayoutBrick(BrickDestroyDefaults):
    """Optional base class providing default state loaders.

    Concrete bricks may subclass this when their state lives in the
    shared workspace state (:mod:`agent_foundry.state`, brick type
    ``state_subdir``) in one of the two standard layouts:

    - **Flat**: one state per instance (sub-instance = instance name).
      Override ``state_subdir`` and ``state_keys`` and the default reads
      the state once.
    - **Nested**: one state per ``<instance_dir>/<subdir>/<sub_instance>``.
      Override ``is_nested_layout`` to ``True``; variables are emitted as
      ``<base_var>__<sub_instance>``.

    Without a workspace client (dry-run / offline) there is no state.

    Bricks with non-standard state (workspace-backed, multi-file, etc.)
    should not subclass this and instead implement
    :meth:`Brick.load_state_outputs` directly.

    Class attributes:
        ``state_subdir``     -- subdirectory under ``instance_dir``.
        ``state_keys``       -- map ``state-file-key -> variable-base-name``.
        ``is_nested_layout`` -- True for per-sub-instance layouts.
    """

    state_subdir: str = ""
    state_keys: dict[str, str] = {}
    is_nested_layout: bool = False

    def _state_parent(self, ctx: DeployContext) -> Path:
        """Directory that holds this brick's state subtree.

        Defaults to ``<instance_dir>/<state_subdir>``.  Payload-owned
        bricks (whose authoring surface moved under
        ``instances/<inst>/payloads/<payload>/``) override this to point
        at the payload root so the deploy-time write and the
        ``load_state_outputs`` read agree on location.
        """
        return ctx.instance_dir / self.state_subdir

    def load_state_outputs(self, ctx: DeployContext) -> dict[str, str]:

        from agent_foundry.state import validate_state

        if not self.state_subdir or not self.state_keys:
            return {}

        parent = self._state_parent(ctx)
        if not parent.is_dir():
            return {}

        result: dict[str, str] = {}
        brick_name = getattr(self, "name", type(self).__name__)
        schema = getattr(self, "state_schema", None)

        from agent_foundry.state import load_state as _ws_load

        if not self.is_nested_layout:
            # Flat layout: one state per instance, keyed by the instance name.
            instance_name = ctx.instance_dir.name
            state = _ws_load(ctx.ws, instance_name, self.state_subdir, instance_name, ctx.target)
            # #156: validate non-empty state against state_schema before
            # extracting outputs so contract violations fail loud here
            # instead of as delayed KeyErrors in downstream bricks.
            # Empty state ({}) is equivalent to "no state yet" (e.g.
            # after a destroy) and is intentionally skipped.  No-op when
            # schema is None.
            if state:
                validate_state(brick_name, state, schema)
            for key, base_var in self.state_keys.items():
                if key in state:
                    result[base_var] = str(state[key])
            return result

        # Nested layout: one state per sub-instance, emit
        # <base_var>__<sub_instance>.
        for child in sorted(parent.iterdir()):
            if not child.is_dir() or child.name.startswith(("_", ".")):
                continue
            state = _ws_load(ctx.ws, ctx.instance_dir.name, self.state_subdir, child.name, ctx.target)
            # #156: validate per-sub-instance so the error pinpoints the
            # offending sub-instance.  Empty dicts (missing state) skip
            # validation so a never-deployed sub-instance is not flagged.
            if state:
                validate_state(brick_name, state, schema, sub_instance=child.name)
            for key, base_var in self.state_keys.items():
                if key in state:
                    result[f"{base_var}__{child.name}"] = str(state[key])
        return result


if not TYPE_CHECKING:
    # Hidden from mypy so unknown attributes of this module stay type errors.

    def __getattr__(name: str) -> type[StandardLayoutBrick]:
        """Keep the old private name importable for forks, with a warning."""
        if name == "_StandardLayoutBrick":
            warnings.warn(
                "agent_foundry.bricks._StandardLayoutBrick is deprecated; subclass StandardLayoutBrick instead.",
                DeprecationWarning,
                stacklevel=2,
            )
            return StandardLayoutBrick
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# ---------------------------------------------------------------------------
# Built-in registry
# ---------------------------------------------------------------------------
#
# Populated incrementally.  68-B lands ``SqlFunctionBrick`` only.  68-C
# adds the other nine and deletes the legacy ``BRICK_DEPLOY_ORDER``
# tuple in ``agent_foundry.deploy._orchestrator``.


def _builtin_bricks() -> tuple[Brick, ...]:
    """Lazily import built-in bricks to avoid circular imports.

    Individual brick modules import ``agent_foundry.bricks`` for the
    protocol; loading them eagerly at module import time would create a
    cycle during ``agent_foundry`` package initialisation.
    """
    from bricks.ai_gateway.brick import AiGatewayBrick
    from bricks.app.brick import AppBrick
    from bricks.data.brick import DataDomainsBrick
    from bricks.genie.brick import GenieSpacesBrick
    from bricks.job.brick import JobsBrick
    from bricks.model_serving.brick import ModelServingBrick
    from bricks.observability.brick import ObservabilityBrick
    from bricks.sql_function.brick import SqlFunctionBrick
    from bricks.tracing.brick import MlflowExperimentBrick
    from bricks.vector_search.brick import VectorIndexesBrick
    from bricks.warehouse.brick import SqlWarehouseBrick

    # Tuple literal of mixed concrete subclasses; cast to the
    # invariant ``tuple[Brick, ...]`` so the registry contract is
    # what callers see.  The runtime tuple is unchanged.
    return cast(
        "tuple[Brick, ...]",
        (
            SqlWarehouseBrick(),
            DataDomainsBrick(),
            SqlFunctionBrick(),
            MlflowExperimentBrick(),
            GenieSpacesBrick(),
            VectorIndexesBrick(),
            ModelServingBrick(),
            JobsBrick(),
            AiGatewayBrick(),
            # Skills and MCP are no longer separate bricks (issue #338):
            # skill validation/assembly and MCP server registration are
            # capabilities of the agent payload, run by
            # ``AgentPayloadAdapter.provision`` via the payload's
            # PROVISION_STEPS manifest (which publishes ``mcp__<sub>``
            # variables before the App renders ``app.yaml``).
            # Lakebase is likewise a payload capability: persistent chat
            # history is provisioned by the Agent App brick when
            # ``[chat_history] enabled = true`` (the default).
            AppBrick(),
            ObservabilityBrick(),
        ),
    )


def _third_party_bricks() -> Iterable[Brick]:
    """Load bricks registered via the ``agent_foundry.bricks`` entry
    point group.  Distributions that do not register anything are
    silently ignored; load failures surface as a RuntimeError because a
    broken third-party brick is worse than a missing one."""
    # ``importlib.metadata.entry_points(group=...)`` is the py3.10+
    # API; the project floor is py3.11 (see pyproject.toml), so the
    # legacy py<3.10 ``entry_points().get(GROUP, [])`` fallback is
    # gone.
    eps = _metadata.entry_points(group=ENTRY_POINT_GROUP)

    for ep in eps:
        try:
            brick_cls = ep.load()
        except Exception as exc:
            raise RuntimeError(
                f"Failed to load agent_foundry.bricks entry point {ep.name!r} = {ep.value!r}: {exc}"
            ) from exc
        # Entry points may expose either the class or an instance;
        # accept both for ergonomics.
        yield brick_cls() if isinstance(brick_cls, type) else brick_cls


def iter_bricks() -> list[Brick]:
    """Return all registered bricks, sorted by (deploy_order, name).

    Built-ins first, then third-party.  When a third-party brick
    declares the same ``name`` as a built-in, the third-party instance
    wins -- this is the intended override path for downstream
    customisation.
    """
    by_name: dict[str, Brick] = {b.name: b for b in _builtin_bricks()}
    for brick in _third_party_bricks():
        by_name[brick.name] = brick
    return sorted(by_name.values(), key=lambda b: (b.deploy_order, b.name))


# ---------------------------------------------------------------------------
# Nested-brick layout -- shared by brick CLIs and destroy planning
# ---------------------------------------------------------------------------


def iter_nested_sub_instances(instance_dir: Path, subdir: str) -> Iterator[Path]:
    """Yield sub-instance directories under ``<instance_dir>/<subdir>/``."""
    parent = instance_dir / subdir
    if not parent.is_dir():
        return
    for child in sorted(parent.iterdir()):
        if child.is_dir() and not child.name.startswith(("_", ".")):
            yield child


def sub_instance_dir(project_root: Path, instance: str, subdir: str, sub: str) -> Path:
    """Canonical path for one sub-instance, whether or not it exists yet.

    ``instances/<instance>/<subdir>/<sub>``.  Use this when writing a new
    sub-instance (e.g. exporting a Genie Space); use
    :func:`resolve_sub_instances` to address existing ones.
    """
    return project_root / "instances" / instance / subdir / sub


def resolve_sub_instances(
    project_root: Path,
    supplied: str,
    subdir: str,
) -> tuple[Path, list[Path]]:
    """Resolve a nested brick's ``--instance`` argument.

    Nested bricks live at ``instances/<app>/<subdir>/<sub>``, so a brick CLI
    needs two directories: the owning instance (which
    :func:`load_brick_variables` keys off, by basename) and the sub-instances to
    act on.  Returns ``(instance_dir, sub_instance_dirs)``.

    *supplied* may be an app name (``my-app``, meaning every sub-instance under
    it), an ``<app>/<sub>`` pair, an ``<app>/<subdir>/<sub>`` triple (the form
    the job brick documents), or a path to either an instance directory or a
    single sub-instance directory.

    Raises ``ValueError`` with a message fit for stderr when nothing matches.
    """
    direct = _existing_dir(project_root, supplied)
    if direct is not None:
        return _classify_dir(direct, subdir)

    app, _, sub = supplied.partition("/")
    sub = sub.removeprefix(f"{subdir}/")
    instance_dir = project_root / "instances" / app
    if not instance_dir.is_dir():
        raise ValueError(f"Instance directory not found: {instance_dir}")

    if not (instance_dir / subdir).is_dir():
        raise ValueError(f"No {subdir}/ directory under {instance_dir}")

    if sub:
        target = instance_dir / subdir / sub
        if not target.is_dir():
            raise ValueError(f"Sub-instance not found: {target}")
        return instance_dir, [target]

    subs = list(iter_nested_sub_instances(instance_dir, subdir))
    if not subs:
        raise ValueError(f"No sub-instances found under {instance_dir / subdir}")
    return instance_dir, subs


def _existing_dir(project_root: Path, supplied: str) -> Path | None:
    """First existing directory *supplied* names, absolute or relative."""
    candidate = Path(supplied)
    if candidate.is_absolute():
        return candidate if candidate.is_dir() else None
    for base in (Path.cwd(), project_root):
        resolved = base / candidate
        if resolved.is_dir():
            return resolved
    return None


def _classify_dir(path: Path, subdir: str) -> tuple[Path, list[Path]]:
    """Interpret an existing directory as an instance, a subdir root, or a sub-instance."""
    if path.parent.name == subdir:
        return path.parent.parent, [path]
    if path.name == subdir:
        subs = list(iter_nested_sub_instances(path.parent, subdir))
        if not subs:
            raise ValueError(f"No sub-instances found under {path}")
        return path.parent, subs
    subs = list(iter_nested_sub_instances(path, subdir))
    if not subs:
        raise ValueError(f"No {subdir}/ sub-instances found under {path}")
    return path, subs


# ---------------------------------------------------------------------------
# load_brick_variables -- shared by every brick's standalone-CLI entrypoint
# (issue #191, F-PR6)
# ---------------------------------------------------------------------------


def load_brick_variables(
    project_root: Path,
    target: str,
    instance_dir: Path,
    *,
    extra_vars: list[str] | None = None,
    ws: Any | None = None,
) -> dict[str, str]:
    """Return the ``${var.*}`` substitution bag for a brick script.

    Composes the instance via :func:`agent_foundry.loader.load_instance_config`
    and returns :meth:`InstanceConfig.to_variables` with the brick script's
    ``--vars`` extras folded in. Deploy-time outputs recorded by previous
    runs (``warehouse_id``, ``genie_space_id__<space>``, ...) are folded in
    afterwards -- see :func:`fill_state_outputs`.

    *instance_dir* is required: brick scripts already compute it, and its
    basename selects the ``[[instances]]`` entry in ``foundry.toml``.
    State outputs are read from the workspace, so they are only folded in
    when *ws* is given.
    """
    resolved_instance_dir = instance_dir.resolve()
    variables = _load_brick_variables_foundry_toml(
        project_root=project_root.resolve(),
        target=target,
        instance_dir=resolved_instance_dir,
        extra_vars=list(extra_vars or []),
    )
    return fill_state_outputs(variables, resolved_instance_dir, target, ws)


def fill_state_outputs(
    variables: dict[str, str],
    instance_dir: Path,
    target: str,
    ws: Any | None,
) -> dict[str, str]:
    """Add deploy-time outputs from previous runs to *variables*.

    Brick CLIs call this once their workspace client exists: the client is
    built from *variables* (profile / host), so state cannot be read while
    they are first loaded.  ``ws=None`` (offline, dry-run) reads no state.

    A deploy gets these from ``_preload_state``, which walks every brick's
    ``load_state_outputs`` before the first brick runs -- that is how
    ``warehouse_id`` reaches a brick whose warehouse was provisioned by an
    earlier run.  Without the same step, a brick invoked through its own CLI
    saw no ``warehouse_id`` and refused to deploy (issue #416).

    Unlike ``_preload_state``, this only fills keys that are absent or empty,
    so an explicit ``--vars warehouse_id=...`` still wins: on a debugging
    escape hatch the value the caller typed should beat persisted state.
    """
    from agent_foundry.config import load_state_outputs
    from agent_foundry.state import BrickStateValidationError

    try:
        outputs = load_state_outputs(instance_dir, target, ws=ws).variables
    except BrickStateValidationError:
        # #156: a state file that violates its schema is a contract
        # failure.  The orchestrator halts on it, so do the same here
        # rather than deploying against half-read state.
        raise
    except Exception as exc:
        # Missing or unreadable state is not fatal here: the brick reports
        # what it actually needs, which is a clearer error than this one.
        print(f"  WARNING: could not read deploy state for {instance_dir.name}: {exc}")
        return variables

    for key, value in outputs.items():
        if value and not variables.get(key):
            variables[key] = value
    return variables


def _load_brick_variables_foundry_toml(
    *,
    project_root: Path,
    target: str,
    instance_dir: Path,
    extra_vars: list[str],
) -> dict[str, str]:
    """Build the variable bag via the new :func:`load_instance_config` path.

    Mirrors the same fold-in logic the deploy verb uses (F-PR5):
    ``to_variables()`` for canonical typed fields, plus free-form
    ``--vars KEY=VALUE`` extras layered on top so user-defined
    ``${var.*}`` keys still flow through to bricks.
    """
    from agent_foundry.loader import (
        FoundryConfigError,
        _parse_extra_vars,
        load_instance_config,
    )

    # The loader picks the instance by directory basename, resolved
    # against the paths declared in ``[[instances]]``.
    instance_name = instance_dir.name

    try:
        cfg = load_instance_config(
            project_root,
            instance=instance_name,
            target_name=target,
            extra_vars=extra_vars or None,
        )
    except FoundryConfigError as exc:
        # Re-raise as a plain ValueError so brick scripts can keep
        # their existing ``except Exception`` blocks without depending
        # on the loader module.
        raise ValueError(str(exc)) from exc

    variables = cfg.to_variables()
    # Free-form extras land last (same precedence as F-PR5's deploy
    # path).
    variables.update(_parse_extra_vars(extra_vars))
    return variables
