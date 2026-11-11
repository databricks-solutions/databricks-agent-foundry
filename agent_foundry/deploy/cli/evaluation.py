"""``agent-foundry eval`` and ``agent-foundry monitors`` (evaluation brick wrappers)."""

from __future__ import annotations

from typing import Any

import typer


def _build_argv(**kwargs: Any) -> list[str]:
    """Convert keyword args into a ``--flag VALUE`` argv list, preserving
    order, and dropping ``None`` / ``False`` flags."""
    argv: list[str] = []
    for key, value in kwargs.items():
        flag = "--" + key.replace("_", "-")
        if value is None:
            continue
        if isinstance(value, bool):
            if value:
                argv.append(flag)
            continue
        if isinstance(value, list):
            for item in value:
                argv.extend([flag, str(item)])
            continue
        argv.extend([flag, str(value)])
    return argv


def eval_cmd(
    instance: str = typer.Option(..., "--instance", help="Instance name."),
    target: str = typer.Option("dev", "--target", help="Deployment target."),
    mode: str = typer.Option(
        "direct",
        "--mode",
        help="Evaluation mode: direct (run agent) or traces (score existing).",
    ),
    predict_via: str = typer.Option(
        "deployed",
        "--predict-via",
        help="In --mode direct: 'deployed' (default) or 'local'.",
    ),
    dataset: str | None = typer.Option(
        None,
        "--dataset",
        help="UC EvaluationDataset / Delta table name; overrides YAML.",
    ),
    max_traces: int = typer.Option(
        100,
        "--max-traces",
        help="Maximum traces to fetch in --mode traces.",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Validate only."),
) -> None:
    """Run MLflow GenAI evaluation for an agent instance."""
    from agent_foundry.deploy._brick_registry import resolve_action

    argv = _build_argv(
        instance=instance,
        target=target,
        mode=mode,
        predict_via=predict_via,
        dataset=dataset,
        max_traces=max_traces,
        dry_run=dry_run,
    )
    raise typer.Exit(code=resolve_action("evaluation", "run")(argv))


_monitors = typer.Typer(
    name="monitors",
    help="Manage MLflow production monitoring scorers.",
    no_args_is_help=True,
)


def _monitors_action(action: str, instance: str, target: str) -> int:
    from agent_foundry.deploy._brick_registry import resolve_action

    return resolve_action("evaluation", "monitors")(
        [
            "--instance",
            instance,
            "--target",
            target,
            "--action",
            action,
        ]
    )


@_monitors.command("start")
def monitors_start(
    instance: str = typer.Option(..., "--instance"),
    target: str = typer.Option("dev", "--target"),
) -> None:
    """Register production monitors."""
    raise typer.Exit(code=_monitors_action("start", instance, target))


@_monitors.command("stop")
def monitors_stop(
    instance: str = typer.Option(..., "--instance"),
    target: str = typer.Option("dev", "--target"),
) -> None:
    """Stop production monitors."""
    raise typer.Exit(code=_monitors_action("stop", instance, target))


@_monitors.command("status")
def monitors_status(
    instance: str = typer.Option(..., "--instance"),
    target: str = typer.Option("dev", "--target"),
) -> None:
    """Show production monitor status."""
    raise typer.Exit(code=_monitors_action("status", instance, target))


@_monitors.command("delete")
def monitors_delete(
    instance: str = typer.Option(..., "--instance"),
    target: str = typer.Option("dev", "--target"),
) -> None:
    """Delete production monitors."""
    raise typer.Exit(code=_monitors_action("delete", instance, target))


def register(app: typer.Typer) -> None:
    """Add this module's commands to the root *app*."""
    app.command("eval")(eval_cmd)
    app.add_typer(_monitors, name="monitors")
