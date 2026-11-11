"""``agent-foundry config`` -- inspect the typed foundry config."""

from __future__ import annotations

from pathlib import Path

import typer

_config_app = typer.Typer(
    name="config",
    help="Inspect the typed foundry config (foundry.toml) for a repo.",
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


@_config_app.command("show")
def config_show_cmd(
    target: str = typer.Option(
        ...,
        "--target",
        help="Target environment name declared in repo-root foundry.toml.",
    ),
    instance: str | None = typer.Option(
        None,
        "--instance",
        help=(
            "Instance name to inspect. Required when the repo declares "
            "more than one [[instances]] entry; optional otherwise."
        ),
    ),
    repo_root: Path = typer.Option(
        None,
        "--repo-root",
        help="Downstream repo root. Defaults to the current working directory.",
    ),
    format: str = typer.Option(
        "toml",
        "--format",
        "-f",
        help="Output format: toml (default), json, or yaml.",
    ),
    resolved: bool = typer.Option(
        False,
        "--resolved",
        help=(
            "Print the runtime InstanceConfig view (post-merge, what "
            "the orchestrator sees) instead of the authored TOML view. "
            "Use this to debug deploys."
        ),
    ),
    include_data: bool = typer.Option(
        False,
        "--include-data",
        help=(
            "Also include data-layer YAMLs (golden datasets, view "
            "definitions, etc.) from the instance directory with "
            "${var.*} substitutions applied. Off by default to keep "
            "output config-focused."
        ),
    ),
    extra_vars: list[str] = typer.Option(
        [],
        "--var",
        help="Extra variable overrides as KEY=VALUE. Repeatable.",
    ),
) -> None:
    """Print the resolved foundry config for a target / instance.

    Default view is the **authored** TOML shape (round-trippable).
    Pass ``--resolved`` to see the runtime ``InstanceConfig`` view
    that the orchestrator consumes at deploy time.
    """
    from agent_foundry.inspect import (
        build_authored_view,
        build_resolved_view,
        render,
    )
    from agent_foundry.loader import FoundryConfigError

    if format not in {"toml", "json", "yaml"}:
        typer.echo(
            f"Unknown --format {format!r}; expected one of: toml, json, yaml.",
            err=True,
        )
        raise typer.Exit(code=2)

    root = (repo_root or Path.cwd()).resolve()
    builder = build_resolved_view if resolved else build_authored_view

    try:
        view = builder(
            root,
            instance=instance,
            target_name=target,
            extra_vars=list(extra_vars) or None,
            include_data=include_data,
        )
    except FoundryConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    # mypy: render's Format is a Literal; we just guarded above.
    typer.echo(render(view, format), nl=False)  # type: ignore[arg-type]


def register(app: typer.Typer) -> None:
    """Add this module's commands to the root *app*."""
    app.add_typer(_config_app, name="config")
