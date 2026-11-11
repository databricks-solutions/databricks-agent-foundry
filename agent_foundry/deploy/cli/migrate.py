"""``agent-foundry migrate`` -- apply framework fixes to project-owned files."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

_migrate_app = typer.Typer(
    name="migrate",
    help="Apply framework fixes to project-owned files that a wheel upgrade cannot reach.",
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


@_migrate_app.command("quote-identifiers")
def migrate_quote_identifiers_cmd(
    instance: Optional[str] = typer.Option(
        None,
        "--instance",
        help="Limit the rewrite to one instance. Default: every instance in the repo.",
    ),
    repo_root: Path = typer.Option(
        None,
        "--repo-root",
        help="Repo root containing instances/. Defaults to the current working directory.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print the rewrite without touching any file.",
    ),
) -> None:
    """Back-quote catalog and schema identifiers in a project's ``.sql`` files.

    A project owns copies of the shipped SQL templates, so upgrading the wheel
    does not fix them.  Unity Catalog allows a hyphen in a catalog name where
    Spark SQL demands a back-quoted identifier, which is why an unquoted
    template fails with ``INVALID_IDENTIFIER`` in some workspaces and not
    others.  Safe to run repeatedly: an already-quoted file is left alone.
    """
    from agent_foundry.migrations import (
        apply_rewrites,
        find_project_sql,
        plan_quote_identifiers,
    )

    root = (repo_root or Path.cwd()).resolve()
    paths = find_project_sql(root, instance)
    if not paths:
        scope = f"instances/{instance}" if instance else "instances"
        typer.echo(f"No .sql files found under {root / scope}")
        raise typer.Exit(code=0)

    rewrites = [rewrite for rewrite in plan_quote_identifiers(paths) if rewrite.changed]
    if not rewrites:
        typer.echo(f"Checked {len(paths)} .sql file(s); every identifier is already quoted.")
        raise typer.Exit(code=0)

    prefix = "[DRY RUN] " if dry_run else ""
    for rewrite in rewrites:
        lines = rewrite.updated.splitlines()
        typer.echo(f"{prefix}{rewrite.path.relative_to(root)}")
        for number in rewrite.changed_line_numbers:
            typer.echo(f"    {number}: {lines[number - 1].strip()}")

    if dry_run:
        typer.echo(f"\n{len(rewrites)} of {len(paths)} file(s) would change. Re-run without --dry-run to apply.")
        raise typer.Exit(code=0)

    apply_rewrites(rewrites)
    typer.echo(f"\nRewrote {len(rewrites)} of {len(paths)} file(s).")


def register(app: typer.Typer) -> None:
    """Add this module's commands to the root *app*."""
    app.add_typer(_migrate_app, name="migrate")
