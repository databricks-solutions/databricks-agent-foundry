"""``agent-foundry brick`` and ``agent-foundry scaffold``."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer


def brick_cmd(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Brick name (e.g. 'warehouse', 'genie')."),
    action: str = typer.Argument(..., help="Action (e.g. 'deploy', 'validate')."),
) -> None:
    """Dispatch into ``bricks/<name>/scripts/<action>.py:main(argv)``."""
    from agent_foundry.deploy._brick_registry import (
        UnknownBrickAction,
        resolve_action,
    )

    try:
        target = resolve_action(name, action)
    except UnknownBrickAction as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2)

    raise typer.Exit(code=target(list(ctx.args)))


# ---------------------------------------------------------------------------
# scaffold -- top-level verb (primary discovery surface, since #205-followup)
# ---------------------------------------------------------------------------


_SCAFFOLD_HELP = (
    "Write a fully-commented starter config for a brick.\n\n"
    "Example: agent-foundry scaffold warehouse --instance my-app\n\n"
    "Run `agent-foundry scaffold --list` to see every available "
    "brick with its display name and a one-line description of what "
    "it does."
)


def scaffold_cmd(
    # Optional argument so we can render a custom error (with the
    # brick list) when it is omitted -- Typer's stock "Missing
    # argument 'BRICK'" does not tell the user what is valid.
    brick: Optional[str] = typer.Argument(
        None,
        help="Brick name. Omit (or pass --list) to see available bricks.",
    ),
    instance: Optional[str] = typer.Option(
        None,
        "--instance",
        help="Instance whose directory receives the scaffolded file(s).",
    ),
    repo_root: Path = typer.Option(
        None,
        "--repo-root",
        help="Repo root (defaults to current working directory).",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Overwrite existing files. Default is skip-with-message.",
    ),
    list_only: bool = typer.Option(
        False,
        "--list",
        help=("Print the table of available bricks and exit.  Useful for shell completion data sources."),
    ),
) -> None:
    """Top-level scaffold verb -- writes a brick's starter config.

    Delegates to :func:`_run_brick_scaffold`.  The ``--list`` flag and
    the custom missing-argument errors are handled inline here since
    they are CLI-shape concerns, not scaffold logic.
    """
    from agent_foundry.scaffold import format_scaffold_listing

    if list_only:
        typer.echo(format_scaffold_listing())
        raise typer.Exit(code=0)

    if brick is None:
        typer.echo(
            f"Missing BRICK argument. Pick one of:\n\n{format_scaffold_listing()}",
            err=True,
        )
        raise typer.Exit(code=2)

    if instance is None:
        typer.echo(
            f"Missing --instance. Example:\n  agent-foundry scaffold {brick} --instance <name>",
            err=True,
        )
        raise typer.Exit(code=2)

    rc = _run_brick_scaffold(
        brick_name=brick,
        instance=instance,
        repo_root=repo_root or Path.cwd(),
        force=force,
    )
    raise typer.Exit(code=rc)


def _run_brick_scaffold(
    *,
    brick_name: str,
    instance: str,
    repo_root: Path,
    force: bool,
) -> int:
    """Core scaffold dispatch behind the top-level ``scaffold`` verb.

    Resolves the instance directory, looks up the per-brick scaffold,
    runs it, and prints a per-file summary.  Returns the process exit
    code (0 success, 2 user error).  Kept argument-style (keyword-only)
    rather than ``argv``-based so unit tests can exercise the dispatch
    without round-tripping through argparse.
    """
    from agent_foundry.scaffold import (
        available_scaffold_names,
        run_scaffold,
    )

    if brick_name not in available_scaffold_names():
        typer.echo(
            f"Unknown brick {brick_name!r}. Bricks with a scaffold: {', '.join(available_scaffold_names())}.",
            err=True,
        )
        return 2

    repo_root = Path(repo_root).resolve()
    instance_dir = repo_root / "instances" / instance
    if not instance_dir.is_dir():
        typer.echo(
            f"Instance directory {instance_dir} does not exist. Create it (or pass --repo-root) before scaffolding.",
            err=True,
        )
        return 2

    result = run_scaffold(brick_name, instance_dir, force=force)

    for path in result.written:
        try:
            rel = path.relative_to(repo_root)
        except ValueError:
            rel = path
        typer.echo(f"  wrote   {rel}")
    for path in result.skipped:
        try:
            rel = path.relative_to(repo_root)
        except ValueError:
            rel = path
        typer.echo(
            f"  skipped {rel} (exists; pass --force to overwrite)",
        )
    for note in result.notes:
        typer.echo(f"  note: {note}")
    return 0


def register(app: typer.Typer) -> None:
    """Add this module's commands to the root *app*."""
    app.command(
        "brick",
        help=(
            "Run a per-brick action (escape hatch).\n\n"
            "Example: agent-foundry brick warehouse deploy --instance my-app\n\n"
            "To scaffold a brick's starter config, use "
            "`agent-foundry scaffold <brick> --instance <name>`."
        ),
        context_settings={
            # Forward unknown flags to the underlying brick script's argv.
            "allow_extra_args": True,
            "ignore_unknown_options": True,
        },
    )(brick_cmd)
    app.command(
        "scaffold",
        help=_SCAFFOLD_HELP,
    )(scaffold_cmd)
