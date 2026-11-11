"""``agent-foundry genie`` -- Genie brick verbs."""

from __future__ import annotations

from pathlib import Path

import typer

_genie = typer.Typer(
    name="genie",
    help="Genie Space helpers (first-class aliases for common brick actions).",
    no_args_is_help=True,
)


@_genie.command("import")
def genie_import_cmd(
    space_id: str = typer.Option(
        ...,
        "--space-id",
        help="Existing Genie Space ID to fetch.",
    ),
    instance: str | None = typer.Option(
        None,
        "--instance",
        help="'<app>/<space>'; writes to instances/<app>/genie_spaces/<space>/.",
    ),
    output: Path | None = typer.Option(
        None,
        "--output",
        help="Explicit output directory (overrides --instance).",
    ),
) -> None:
    """Fetch an existing Genie Space and decompose it into the repo layout.

    Thin first-class alias for ``agent-foundry brick genie export ...``.
    """
    from agent_foundry.deploy._brick_registry import resolve_action

    argv: list[str] = ["--space-id", space_id]
    if instance:
        argv.extend(["--instance", instance])
    if output is not None:
        argv.extend(["--output", str(output)])
    raise typer.Exit(code=resolve_action("genie", "export")(argv))


def register(app: typer.Typer) -> None:
    """Add this module's commands to the root *app*."""
    app.add_typer(_genie, name="genie")
