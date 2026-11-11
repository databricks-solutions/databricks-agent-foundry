"""``agent-foundry sync-schemas`` and ``agent-foundry validate``."""

from __future__ import annotations

from pathlib import Path

import typer


def sync_schemas_cmd(
    target_dir: Path = typer.Option(
        Path(".agent-foundry") / "schemas",
        "--target-dir",
        help=("Directory in the downstream repo to write schemas into. Relative paths resolve against --repo-root."),
    ),
    repo_root: Path = typer.Option(
        None,
        "--repo-root",
        help="Downstream repo root. Defaults to the current working directory.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Overwrite existing schema files. Without this flag, "
        "sync-schemas refuses to clobber an existing target dir "
        "that contains files we don't recognise.",
    ),
) -> None:
    """Copy the JSON Schemas shipped in this wheel into a downstream repo.

    Downstream YAML/TOML files reference these via
    ``# yaml-language-server: $schema=...`` headers so editors
    autocomplete and validate against the framework's typed
    contracts.  Re-run this verb after every framework upgrade so the
    on-disk schemas track the installed wheel version.
    """
    from agent_foundry.schemas import SCHEMA_BUNDLE, generate_all_schemas

    root = (repo_root or Path.cwd()).resolve()
    dest = target_dir if target_dir.is_absolute() else (root / target_dir).resolve()

    if dest.exists() and not force:
        unknown = [p.name for p in dest.iterdir() if p.is_file() and p.name not in SCHEMA_BUNDLE]
        if unknown:
            typer.echo(
                f"Refusing to write into {dest}: it contains files not "
                f"managed by sync-schemas ({', '.join(sorted(unknown))}). "
                "Pass --force to overwrite anyway, or pick a different "
                "--target-dir.",
                err=True,
            )
            raise typer.Exit(code=2)

    generate_all_schemas(out_dir=dest)
    typer.echo(
        f"Synced {len(SCHEMA_BUNDLE)} schema(s) into {dest.relative_to(root) if dest.is_relative_to(root) else dest}."
    )


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


def validate_cmd(
    workspace: bool = typer.Option(
        False,
        "--workspace",
        help="Include workspace connectivity / permission checks.",
    ),
    target: str = typer.Option(
        "dev",
        "--target",
        help="Target name to load variables from when --workspace is set.",
    ),
    offline: bool = typer.Option(
        True,
        "--offline/--no-offline",
        help="Run offline (local) checks (default).",
    ),
) -> None:
    """Check local + workspace prerequisites for an Agent Bricks deploy."""
    from agent_foundry.validate import run_checks

    rc = run_checks(offline=offline, workspace=workspace, target=target)
    raise typer.Exit(code=rc)


def register(app: typer.Typer) -> None:
    """Add this module's commands to the root *app*."""
    app.command("sync-schemas")(sync_schemas_cmd)
    app.command("validate")(validate_cmd)
