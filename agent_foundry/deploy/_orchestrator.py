"""
Deployment orchestrator
========================
Library-grade deploy loop.  Moved from ``scripts/deploy_all.py`` in
68-A (issue #81) so that downstream consumers can install the wheel
and call :func:`deploy_instance` without the repo-root ``scripts/``
directory on their PYTHONPATH.

68-C (issue #84) replaces the static ``BRICK_DEPLOY_ORDER`` tuple with
a registry lookup (:func:`agent_foundry.bricks.iter_bricks`) so that
adding a new brick no longer requires editing this module.

Path policy:

- No module-level ``Path(__file__)`` arithmetic. The framework does not
  guess its own install location (68-A).
- Callers pass an explicit ``instance_dir``; ``instances_root`` is a
  CLI-side concern.
- Brick package data is loaded via :mod:`importlib.resources` inside
  each brick's own script module.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from agent_foundry.bricks import Brick, BrickResult, DeployContext, DestroyContext, DestroyPlan, iter_bricks
from agent_foundry.config import InstanceConfig
from agent_foundry.deploy._brick_registry import cli_name_for
from agent_foundry.deploy.gone import verify_gone
from agent_foundry.public import DeployResult, DestroyResult

# ---------------------------------------------------------------------------
# Instance discovery
# ---------------------------------------------------------------------------


def discover_instances(instances_root: Path) -> list[Path]:
    """Return directories under *instances_root* that look like instances.

    An instance is a directory with a per-instance ``foundry.toml``
    (issue #295).

    Callers must supply the root explicitly. The framework does not walk
    up the filesystem looking for an ``instances/`` sibling: per 68-A,
    path guessing is the caller's (or the CLI's) responsibility.
    """
    from agent_foundry._instance_marker import is_instance_dir

    if not instances_root.is_dir():
        return []
    return sorted(d for d in instances_root.iterdir() if d.is_dir() and is_instance_dir(d))


def resolve_deployer(
    variables: dict[str, str],
    dry_run: bool = False,
    *,
    offline: bool = False,
    ws: Any | None = None,
) -> dict[str, str]:
    """If deployer is not set, resolve from the injected workspace client.

    Never constructs a WorkspaceClient. Callers that already built one
    for the run pass *ws*; dry-run and offline use ``dry_run_user``.
    """
    if variables.get("deployer"):
        return variables
    if dry_run or offline or ws is None:
        if dry_run or offline:
            variables.setdefault("deployer", "dry_run_user")
        return variables
    try:
        me = ws.current_user.me()
        variables["deployer"] = me.user_name
        print(f"  Resolved deployer: {me.user_name}")
    except Exception:
        pass
    return variables


# ---------------------------------------------------------------------------
# Registry dispatch
# ---------------------------------------------------------------------------


def _ctx(
    instance_dir: Path,
    target: str,
    variables: dict[str, str],
    dry_run: bool,
    ws: Any | None = None,
    config: InstanceConfig | None = None,
) -> DeployContext:
    """Build a :class:`DeployContext` for one brick invocation.

    ``config`` carries the typed runtime :class:`InstanceConfig` when
    the orchestrator's caller has it available (the CLI always does).
    Bricks should prefer
    ``ctx.config.<field>`` over ``ctx.variables[...]`` for strongly
    typed access; the variable bag remains the legacy fallback.
    """
    return DeployContext(
        instance_dir=instance_dir,
        target=target,
        variables=variables,
        dry_run=dry_run,
        ws=ws,
        config=config,
    )


def _try_get_workspace_client(
    variables: dict[str, str],
    *,
    ws: Any | None = None,
    offline: bool = False,
) -> Any | None:
    """Return a workspace client when credentials are available.

    Used for read-only state access during dry-run as well as live
    deploy/destroy.  Bricks that persist state workspace-only (Genie,
    MCP) need ``ctx.ws`` set even in preview mode so destroy plans and
    downstream variable substitution reflect live resources.  Returns
    ``None`` when *offline*, when the unit-test skip env is set, or
    when auth is unavailable.  Callers that already constructed a
    client pass *ws* so this helper does not call the factory again.
    """
    if offline:
        return None
    if ws is not None:
        return ws
    from agent_foundry.workspace_client import skip_default_workspace_client

    if skip_default_workspace_client():
        return None
    try:
        from agent_foundry.workspace_client import get_workspace_client

        return get_workspace_client(variables)
    except Exception:
        return None


def workspace_client_for_run(
    variables: dict[str, str],
    *,
    offline: bool = False,
) -> Any | None:
    """One client per deploy/destroy/eval CLI invocation. ``None`` when offline."""
    return _try_get_workspace_client(variables, offline=offline)


def _dispatch_brick(
    brick: Brick,
    instance_dir: Path,
    target: str,
    variables: dict[str, str],
    dry_run: bool,
    *,
    ws: Any | None = None,
    config: InstanceConfig | None = None,
) -> list[BrickResult]:
    """Call ``brick.deploy(ctx)`` and return the typed
    :class:`~agent_foundry.bricks.BrickResult` list as-is.

    Powers the typed :func:`deploy_instance` return value
    (:class:`DeployResult.bricks`).

    ``config`` carries the typed runtime
    :class:`~agent_foundry.config.InstanceConfig` when available.
    Forwarded into ``ctx.config`` so bricks can read fields like
    ``ctx.config.chat_history.enabled`` instead of munging strings
    out of the variable bag.

    Any ``ctx.variables`` mutations the brick made during ``deploy``
    are merged back into the caller's ``variables`` dict so downstream
    bricks see upstream outputs (e.g. the warehouse brick publishing
    the freshly-created ``warehouse_id``).  Since 68-B,
    ``DeployContext`` is a Pydantic model and Pydantic shallow-copies
    dict fields on construction, so ``ctx.variables is not variables``
    and we must merge explicitly.
    """
    ctx = _ctx(
        instance_dir,
        target,
        variables,
        dry_run,
        ws=ws,
        config=config,
    )
    if not brick.applies_to(ctx):
        return []
    results = list(brick.deploy(ctx))
    variables.update(ctx.variables)
    return results


# ---------------------------------------------------------------------------
# State preload
# ---------------------------------------------------------------------------


def _preload_state(
    instance_dir: Path,
    target: str,
    variables: dict[str, str],
    dry_run: bool = False,
    *,
    config: InstanceConfig | None = None,
    ws: Any | None = None,
    offline: bool = False,
) -> None:
    """Pre-load outputs from previous deployments so downstream bricks
    can reference them even when upstream bricks are skipped.

    Iterates every registered brick and merges its declared outputs.
    Replaces the old centralized ``STATE_KEY_MAPS`` (deleted in 68-C).

    ``config`` is forwarded to each brick's ``load_state_outputs``
    via ``ctx.config`` so bricks that gate on typed config (e.g.
    ``chat_history.enabled``) can decide whether to bother reading
    state at all.
    """
    ws = _try_get_workspace_client(variables, ws=ws, offline=offline)

    from agent_foundry.state import BrickStateValidationError

    for brick in iter_bricks():
        ctx = _ctx(instance_dir, target, variables, dry_run, ws=ws, config=config)
        try:
            outputs = brick.load_state_outputs(ctx)
        except BrickStateValidationError:
            # #156: state_schema violations are a contract failure, not
            # a transient I/O hiccup.  Re-raise so the operator sees a
            # clear message and the deploy halts before a downstream
            # brick consumes invalid state.
            raise
        except Exception as exc:
            print(f"  WARNING: load_state_outputs failed for brick {brick.name!r}: {exc}")
            continue
        variables.update(outputs)


# ---------------------------------------------------------------------------
# Instance-level orchestrator
# ---------------------------------------------------------------------------


def deploy_instance(
    instance_dir: Path,
    target: str,
    *,
    variables: dict[str, str] | None = None,
    dry_run: bool = False,
    skip_bricks: set[str] | None = None,
    only_bricks: set[str] | None = None,
    instance_config: InstanceConfig | None = None,
    ws: Any | None = None,
    offline: bool = False,
) -> DeployResult:
    """Deploy all bricks for a single application instance.

    Parameters
    ----------
    instance_dir
        Path to the instance directory (containing ``foundry.toml``;
        see issue #295).
    target
        Deployment target name (e.g. ``"dev"``, ``"staging"``, ``"prod"``).
    variables
        Pre-loaded variable dict. When omitted, callers are responsible
        for supplying one -- the CLI (:mod:`agent_foundry.deploy.cli`)
        loads it from ``foundry.toml`` (typed contract, #191).
    dry_run
        If True, bricks assemble + validate but never mutate. Operator
        dry-run may still read live workspace state unless *offline*.
    ws
        Optional pre-constructed workspace client. When omitted (and
        not *offline*), the orchestrator constructs one.
    offline
        Never construct a workspace client. Hermetic CI / unit tests
        and ``--offline`` CLI use this; *ws* is ignored.
    skip_bricks
        Optional set of lowercase brick name-or-display-name substrings
        to skip (the ``--skip`` CLI flag feeds this).
    only_bricks
        Optional allowlist of lowercase brick name-or-display-name
        substrings (the ``--only`` CLI flag feeds this). When non-empty,
        every brick that does not match is skipped. Mutually exclusive
        with ``skip_bricks`` at the CLI layer.
    instance_config
        Typed runtime :class:`InstanceConfig` for the deploying
        instance.  Forwarded to every brick via ``ctx.config``; the
        CLI always supplies it (built by the loader).  ``None`` is tolerated for
        backwards compatibility with hermetic test paths and any
        third-party callers from before this kwarg existed -- bricks
        that read ``ctx.config`` must guard for that.

    Returns
    -------
    DeployResult
        Typed wrapper around the per-brick results.  Use
        ``result.bricks[display_name]`` for the per-brick
        :class:`~agent_foundry.bricks.BrickResult` list.

    Notes
    -----
    Before running any brick, state files from previous deployments are
    loaded so that resource outputs (e.g. ``genie_space_id``) are
    available even when the producing brick is skipped.  The variables
    dict is mutated in place so downstream bricks see upstream outputs.

    Deploy order comes from the registry: every registered ``Brick``'s
    ``deploy_order`` key sorted ascending (ties broken by ``name``).
    """
    instance_name = instance_dir.name
    skip_bricks = skip_bricks or set()
    only_bricks = only_bricks or set()
    if variables is None:
        variables = {}

    print(f"\n{'=' * 50}")
    print(f"Instance: {instance_name}")
    print(f"{'=' * 50}")

    from agent_foundry.payload_requirements import enforce_payload_requirements

    enforce_payload_requirements(
        instance_dir,
        variables,
        skip_bricks=skip_bricks,
        only_bricks=only_bricks,
    )

    ws = _try_get_workspace_client(variables, ws=ws, offline=offline)

    _preload_state(
        instance_dir,
        target,
        variables,
        dry_run=dry_run,
        config=instance_config,
        ws=ws,
        offline=offline,
    )

    bricks_results: dict[str, list[BrickResult]] = {}
    for brick in iter_bricks():
        display = getattr(brick, "display_name", brick.name)

        haystack_parts = (
            display.lower(),
            brick.name.lower(),
            cli_name_for(brick.name).lower(),
        )
        if any(s in part for s in skip_bricks for part in haystack_parts):
            print(f"\n--- {display} [SKIPPED] ---")
            bricks_results[display] = [
                BrickResult(name="*", status="skipped", rc=0),
            ]
            continue

        if only_bricks and not any(s in part for s in only_bricks for part in haystack_parts):
            print(f"\n--- {display} [SKIPPED] ---")
            bricks_results[display] = [
                BrickResult(name="*", status="skipped", rc=0),
            ]
            continue

        print(f"\n--- {display} ---")
        bricks_results[display] = _dispatch_brick(
            brick,
            instance_dir,
            target,
            variables,
            dry_run,
            ws=ws,
            config=instance_config,
        )

    result = DeployResult(
        instance_name=instance_name,
        target=target,
        bricks=bricks_results,
    )
    if not dry_run and not offline:
        from agent_foundry.telemetry import emit_lifecycle_telemetry

        emit_lifecycle_telemetry(
            ws,
            cmd="deploy",
            bricks=bricks_results,
            instance_dir=instance_dir,
        )
    return result


def _destroy_ctx(
    instance_dir: Path,
    target: str,
    variables: dict[str, str],
    dry_run: bool,
    force: bool,
    ws: Any | None = None,
    config: InstanceConfig | None = None,
) -> DestroyContext:
    return DestroyContext(
        instance_dir=instance_dir,
        target=target,
        variables=variables,
        dry_run=dry_run,
        force=force,
        ws=ws,
        config=config,
    )


def collect_destroy_plan(
    instance_dir: Path,
    target: str,
    *,
    variables: dict[str, str] | None = None,
    dry_run: bool = False,
    force: bool = False,
    bricks: list[Brick] | None = None,
    instance_config: InstanceConfig | None = None,
    ws: Any | None = None,
    offline: bool = False,
) -> DestroyPlan:
    """Aggregate :meth:`Brick.plan_destroy` across all registered bricks."""
    variables = dict(variables or {})
    ws = _try_get_workspace_client(variables, ws=ws, offline=offline)

    _preload_state(
        instance_dir,
        target,
        variables,
        dry_run=dry_run,
        config=instance_config,
        ws=ws,
        offline=offline,
    )

    ctx = _destroy_ctx(
        instance_dir,
        target,
        variables,
        dry_run,
        force,
        ws=ws,
        config=instance_config,
    )
    items = []
    for brick in bricks or list(reversed(iter_bricks())):
        items.extend(brick.plan_destroy(ctx).items)
    return DestroyPlan(items=items)


def destroy_instance(
    instance_dir: Path,
    target: str,
    *,
    variables: dict[str, str] | None = None,
    dry_run: bool = False,
    force: bool = False,
    skip_bricks: set[str] | None = None,
    instance_config: InstanceConfig | None = None,
    bricks: list[Brick] | None = None,
    ws: Any | None = None,
    offline: bool = False,
) -> DestroyResult:
    """Tear down resources created by :func:`deploy_instance`.

    Bricks run in reverse ``deploy_order``.  Unlike deploy, every
    registered brick is visited regardless of :meth:`Brick.applies_to`;
    each brick decides from persisted state whether work remains.

    State outputs are pre-loaded into *variables* before any deletion
    so cross-references remain available for the whole run.
    """
    instance_name = instance_dir.name
    skip_bricks = skip_bricks or set()
    variables = dict(variables or {})
    t0 = time.time()

    print(f"\n{'=' * 50}")
    print(f"Instance: {instance_name}")
    print(f"{'=' * 50}")

    ws = _try_get_workspace_client(variables, ws=ws, offline=offline)

    _preload_state(
        instance_dir,
        target,
        variables,
        dry_run=dry_run,
        config=instance_config,
        ws=ws,
        offline=offline,
    )

    ctx = _destroy_ctx(
        instance_dir,
        target,
        variables,
        dry_run,
        force,
        ws=ws,
        config=instance_config,
    )

    brick_list = bricks or list(reversed(iter_bricks()))
    plan = DestroyPlan(items=[item for brick in brick_list for item in brick.plan_destroy(ctx).items])

    bricks_results: dict[str, list[BrickResult]] = {}
    for brick in brick_list:
        display = getattr(brick, "display_name", brick.name)
        haystack_parts = (
            display.lower(),
            brick.name.lower(),
            cli_name_for(brick.name).lower(),
        )
        if any(s in part for s in skip_bricks for part in haystack_parts):
            print(f"\n--- {display} [SKIPPED] ---")
            bricks_results[display] = [BrickResult(name="*", status="skipped", rc=0)]
            continue

        print(f"\n--- {display} ---")
        results = list(brick.destroy(ctx))
        if ws is not None and not dry_run:
            results = verify_gone(ws, results)
        if not results:
            results = [
                BrickResult(
                    name=brick.name,
                    status="skipped",
                    rc=0,
                    details={"reason": "no destroy results"},
                )
            ]
        bricks_results[display] = results

    duration = time.time() - t0
    result = DestroyResult(
        instance_name=instance_name,
        target=target,
        bricks=bricks_results,
        plan=plan,
        forced=force,
        duration_seconds=duration,
    )
    if not dry_run and not offline:
        from agent_foundry.telemetry import emit_lifecycle_telemetry

        emit_lifecycle_telemetry(
            ws,
            cmd="destroy",
            bricks=bricks_results,
            instance_dir=instance_dir,
        )
    return result
