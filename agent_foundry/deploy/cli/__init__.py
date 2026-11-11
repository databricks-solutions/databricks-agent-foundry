"""
``agent-foundry`` CLI
======================
Typer-based front door for every developer workflow (issue #96).

The CLI is intentionally dumb: each subcommand is a thin wrapper that
parses flags and calls an existing library function (or a brick
script's ``main(argv)``). All business logic lives in
:mod:`agent_foundry.deploy._orchestrator` and the per-brick scripts.

Path resolution (issue #81, 68-A):

- ``foundry.toml`` is read from ``<cwd>``.
- ``--instances-root`` defaults to ``<cwd>/instances``.
- Neither path is discovered by walking up the filesystem looking for
  framework-shaped directories.

Entry point: ``agent-foundry`` (declared in ``pyproject.toml``).
"""

from __future__ import annotations

import typer

from agent_foundry.deploy.cli import (
    call,
    config,
    deploy,
    destroy,
    evaluation,
    genie,
    init,
    migrate,
    run,
    scaffold,
    validate,
    verify,
)

app = typer.Typer(
    name="agent-foundry",
    help="Deploy and operate Agent Bricks on Databricks.",
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


@app.callback()
def _root_callback() -> None:
    """Force typer into multi-command mode.

    Without an explicit callback, a Typer app with a single ``@app.command``
    treats that command as the default and rejects the subcommand name on
    the CLI.  Adding an empty callback makes the app expect a subcommand
    even before all the verbs land.
    """


# Registration order is the order ``--help`` lists the commands in.
deploy.register(app)
config.register(app)
migrate.register(app)
validate.register(app)
evaluation.register(app)
verify.register(app)
run.register(app)
call.register(app)
genie.register(app)
scaffold.register(app)
destroy.register(app)
init.register(app)


# ---------------------------------------------------------------------------
# Console-script entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Public entry point referenced by ``[project.scripts]``.

    Wraps the typer app so ``main(argv)`` keeps its historical
    ``-> int`` contract.  Click raises ``SystemExit`` for every
    terminal path (success, errors, ``--help``) so we catch it and
    return the code instead.
    """
    try:
        app(args=argv, prog_name="agent-foundry")
    except SystemExit as exc:
        if exc.code is None:
            return 0
        if isinstance(exc.code, int):
            return exc.code
        return 1 if exc.code else 0
    return 0
