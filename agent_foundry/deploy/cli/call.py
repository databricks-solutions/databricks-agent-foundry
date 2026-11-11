"""``agent-foundry call`` -- programmatic A2A / MCP callers."""

from __future__ import annotations

import sys

import click
import typer

_call = typer.Typer(
    name="call",
    help="Call a deployed agent via A2A or MCP (requires the 'call' extra).",
    no_args_is_help=True,
)


def _resolve_call_target(
    *,
    instance: str | None,
    target: str,
    app_host: str | None,
    profile: str | None,
) -> tuple[str, str | None]:
    """Translate CLI flags into ``(app_host, profile)`` for the call lib.

    Wraps :func:`agent_foundry.cli.call.resolve_app_host` and adds a few
    stderr diagnostics that the silent library function deliberately
    avoids:

    * Note when ``--app-host`` is given alongside ``--instance`` (the
      explicit host wins, the instance/target are ignored).
    * ``ValueError`` from the resolver is turned into a Click
      ``UsageError`` (exit code 2, standard ``Usage: ... / Error: ...``
      formatting).
    """
    from agent_foundry.cli.call import resolve_app_host

    if app_host and instance:
        typer.echo("note: --app-host given, ignoring --instance/--target", err=True)

    try:
        return resolve_app_host(
            instance=instance,
            target=target,
            profile=profile,
            app_host=app_host,
        )
    except ValueError as exc:
        raise click.UsageError(str(exc)) from exc


def _resolve_call_message(message: str | None) -> str:
    """Resolve MESSAGE for ``call_*`` from positional / stdin / prompt.

    Order of resolution:

    1. Positional ``message`` (when non-empty and not the literal ``"-"``).
    2. ``sys.stdin`` when piped (``isatty() == False``).
    3. Interactive ``Question:`` prompt.

    Raises :class:`click.UsageError` when none of the three yields a
    non-empty message.
    """
    if message and message != "-":
        return message

    if not sys.stdin.isatty():
        from_stdin = sys.stdin.read().strip()
        if from_stdin:
            return from_stdin
        raise click.UsageError("MESSAGE is empty (read from stdin)")

    prompted: str = typer.prompt("Question").strip()
    if not prompted:
        raise click.UsageError("MESSAGE cannot be empty")
    return prompted


_MESSAGE_ARG = typer.Argument(
    None,
    help="Question to ask the agent. Reads from stdin when piped, or prompts interactively when omitted.",
)
_INSTANCE_OPT = typer.Option(
    None,
    "--instance",
    help="Instance name (resolves the deployed app host via apps.get).",
)
_TARGET_OPT = typer.Option(
    "dev",
    "--target",
    help="Target environment (used with --instance).",
)
_APP_HOST_OPT = typer.Option(
    None,
    "--app-host",
    help="App hostname override (e.g. my-agent.cloud.databricksapps.com).",
)
_PROFILE_OPT = typer.Option(
    None,
    "--profile",
    help="Databricks CLI profile (default: from target config).",
)


@_call.command("a2a")
def call_a2a_cmd(
    message: str | None = _MESSAGE_ARG,
    instance: str | None = _INSTANCE_OPT,
    target: str = _TARGET_OPT,
    app_host: str | None = _APP_HOST_OPT,
    profile: str | None = _PROFILE_OPT,
) -> None:
    """Stream a response from the agent over the A2A protocol."""
    from agent_foundry.cli.call import call_a2a

    resolved_message = _resolve_call_message(message)
    host, resolved_profile = _resolve_call_target(
        instance=instance,
        target=target,
        app_host=app_host,
        profile=profile,
    )
    raise typer.Exit(
        code=call_a2a(
            app_host=host,
            profile=resolved_profile,
            message=resolved_message,
        )
    )


@_call.command("mcp")
def call_mcp_cmd(
    message: str | None = _MESSAGE_ARG,
    instance: str | None = _INSTANCE_OPT,
    target: str = _TARGET_OPT,
    app_host: str | None = _APP_HOST_OPT,
    profile: str | None = _PROFILE_OPT,
) -> None:
    """Single-turn ``ask_agent`` call via MCP."""
    from agent_foundry.cli.call import call_mcp

    resolved_message = _resolve_call_message(message)
    host, resolved_profile = _resolve_call_target(
        instance=instance,
        target=target,
        app_host=app_host,
        profile=profile,
    )
    raise typer.Exit(
        code=call_mcp(
            app_host=host,
            profile=resolved_profile,
            message=resolved_message,
        )
    )


def register(app: typer.Typer) -> None:
    """Add this module's commands to the root *app*."""
    app.add_typer(_call, name="call")
