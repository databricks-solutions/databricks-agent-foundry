"""``agent-foundry destroy`` -- tear down deployed resources."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import typer

from agent_foundry.deploy._orchestrator import (
    collect_destroy_plan,
    destroy_instance,
    resolve_deployer,
    workspace_client_for_run,
)
from agent_foundry.deploy.cli.deploy import instance_dir_for
from agent_foundry.public import DestroyResult


def _prompt_for_force(destructive_items: list[Any]) -> bool:
    """Ask on TTY whether to proceed with destructive teardown."""
    if not destructive_items:
        return False
    if not sys.stdin.isatty():
        return False
    print("\nThe following resources require --force to delete:")
    for item in destructive_items:
        reason = f" ({item.reason})" if item.reason else ""
        print(f"  - [{item.brick}] {item.resource}{reason}")
    answer = input("\nContinue with safe destroy (skip destructive items)? [Y/n] ").strip().lower()
    return answer in ("n", "no")


def _summarise_destroy(result: DestroyResult) -> int:
    print(f"\n{'=' * 50}")
    print(f"Destroy Summary ({result.target}, {result.duration_seconds:.1f}s)")
    print(f"{'=' * 50}\n")
    has_errors = False
    for display, brick_results in result.bricks.items():
        for br in brick_results:
            reason = ""
            if br.status == "ok":
                label = "OK"
            elif br.status == "skipped":
                label = "SKIP"
                reason = f" ({br.details['reason']})" if br.details.get("reason") else ""
            else:
                label = "ERROR"
                has_errors = True
            print(f"  [{label}] {display}/{br.name}{reason}")
    return 1 if has_errors else 0


def _run_destroy(
    *,
    instance: str,
    target: str,
    instances_root: Path | None,
    dry_run: bool,
    force: bool,
    brick: str | None,
    extra_vars: list[str],
    offline: bool = False,
) -> int:
    from agent_foundry._legacy_layout import reject_legacy_layout
    from agent_foundry.bricks import iter_bricks
    from agent_foundry.deploy._brick_registry import deploy_name_for, list_bricks
    from agent_foundry.loader import FoundryConfigError, load_instance_config, load_repo_config

    cwd = Path.cwd()
    instances_root_p = instances_root or (cwd / "instances")

    brick_filter: str | None = None
    if brick:
        brick_filter = deploy_name_for(brick)
        if brick_filter is None:
            typer.echo(
                f"Unknown brick {brick!r}. Choose from: {', '.join(list_bricks())}",
                err=True,
            )
            return 2

    if offline and not dry_run:
        print("\nERROR: --offline requires --dry-run (offline is a hermetic plan; it cannot mutate).")
        return 1

    print(f"=== Agent Bricks: Destroy ({target}) ===")
    if dry_run and offline:
        print("  Mode: DRY RUN (offline, no workspace)")
    elif dry_run:
        print("  Mode: DRY RUN (may read live workspace state)")
    if force:
        print("  Mode: FORCE (destructive resources enabled)")
    print(f"  Instances root: {instances_root_p}")
    print()

    try:
        reject_legacy_layout(cwd, instances_root_p)
        repo = load_repo_config(cwd)
        cfg = load_instance_config(
            cwd,
            instance=instance,
            target_name=target,
            extra_vars=extra_vars or None,
        )
    except FoundryConfigError as exc:
        print(f"ERROR: {exc}")
        return 1

    bricks_list = list(reversed(iter_bricks()))
    if brick_filter:
        bricks_list = [b for b in bricks_list if b.name == brick_filter]

    instance_dir = instance_dir_for(cwd, repo, instance)
    variables = cfg.to_variables()
    ws = workspace_client_for_run(variables, offline=offline)
    variables = resolve_deployer(variables, dry_run=dry_run, offline=offline, ws=ws)

    plan = collect_destroy_plan(
        instance_dir,
        target,
        variables=variables,
        dry_run=dry_run,
        force=force,
        bricks=bricks_list,
        instance_config=cfg,
        ws=ws,
        offline=offline,
    )
    destructive = [item for item in plan.items if item.destructive]
    effective_force = force
    if destructive and not force and _prompt_for_force(destructive):
        print("Aborted.")
        return 1

    result = destroy_instance(
        instance_dir,
        target,
        variables=variables,
        dry_run=dry_run,
        force=effective_force,
        instance_config=cfg,
        bricks=bricks_list,
        ws=ws,
        offline=offline,
    )
    rc = _summarise_destroy(result)
    if rc == 0:
        print("\nDestroy completed.")
    else:
        print("\nDestroy completed with errors.")
    return rc


def destroy_cmd(
    instance: str = typer.Option(
        ...,
        "--instance",
        help="Instance whose resources should be torn down.",
    ),
    target: str = typer.Option(
        "dev",
        "--target",
        help="Target name (used to locate state files).",
    ),
    brick: str | None = typer.Option(
        None,
        "--brick",
        help="Destroy only this brick (accepts deploy-registry or CLI name).",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Preview teardown. Still reads live workspace state unless --offline.",
    ),
    offline: bool = typer.Option(
        False,
        "--offline",
        help="Hermetic plan: never construct a WorkspaceClient. Requires --dry-run.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Allow destructive teardown (UC schemas, SQL functions, Lakebase, trace tables).",
    ),
    instances_root: Path | None = typer.Option(
        None,
        "--instances-root",
        help="Directory containing instance subdirs. Defaults to <cwd>/instances.",
    ),
    extra_vars: list[str] | None = typer.Option(
        None,
        "--vars",
        help="Extra variable as key=value (repeat for multiple).",
    ),
) -> None:
    """Tear down resources created by ``agent-foundry deploy``.

    Iterates registered bricks in reverse deploy order via
    :func:`agent_foundry.destroy_instance`.  Destructive resources
    (UC schemas, SQL functions, Lakebase, trace tables) require
    ``--force`` unless you confirm skipping them interactively.
    """
    rc = _run_destroy(
        instance=instance,
        target=target,
        instances_root=instances_root,
        dry_run=dry_run,
        force=force,
        brick=brick,
        extra_vars=extra_vars or [],
        offline=offline,
    )
    raise typer.Exit(code=rc)


def register(app: typer.Typer) -> None:
    """Add this module's commands to the root *app*."""
    app.command("destroy")(destroy_cmd)
