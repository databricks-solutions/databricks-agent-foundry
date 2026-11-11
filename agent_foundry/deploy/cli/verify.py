"""``agent-foundry test``, ``wait`` and ``smoke``."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import typer

from agent_foundry.deploy.cli.deploy import pytest_subprocess_env


def test_cmd(
    offline: bool = typer.Option(False, "--offline", help="Offline tests only."),
    live: bool = typer.Option(
        False,
        "--live",
        help="Add -m live --run-live (tests/live checks against a CLI deployment).",
    ),
    component: bool = typer.Option(
        False,
        "--component",
        help="Add --run-component (packaging / packaged-CLI smokes).",
    ),
    instance: str | None = typer.Option(
        None,
        "--instance",
        help="Limit to instances/<name>/tests/ if provided.",
    ),
    target: str = typer.Option("dev", "--target"),
) -> None:
    """Run the Agent Bricks test suite (pytest passthrough)."""
    cmd: list[str] = [sys.executable, "-m", "pytest", "-v", "--tb=short"]
    if live:
        cmd.extend(["-m", "live", "--run-live"])
    if component:
        cmd.extend(["-m", "component", "--run-component"])
    if instance:
        path = Path.cwd() / "instances" / instance / "tests"
        if path.exists():
            cmd.append(str(path))
    if offline and not live and not component:
        cmd.extend(["-m", "not live and not component"])

    subprocess_env = pytest_subprocess_env(Path.cwd(), target)
    raise typer.Exit(code=subprocess.run(cmd, env=subprocess_env).returncode)


def _run_wait(*, instance: str, target: str, timeout_seconds: float) -> int:
    """Poll the instance's Databricks App until it is running; print its URL."""
    from agent_foundry.loader import load_instance_config
    from agent_foundry.testing.readiness import ReadinessError, wait_for_app
    from agent_foundry.workspace_client import get_workspace_client, load_target_auth_variables

    cwd = Path.cwd()
    try:
        cfg = load_instance_config(cwd, instance=instance, target_name=target)
    except Exception as exc:
        print(f"ERROR: could not load instance {instance!r} for target {target!r}: {exc}")
        return 1
    ws = get_workspace_client(load_target_auth_variables(cwd, target))
    try:
        url = wait_for_app(ws, cfg.app_resource_name, timeout_seconds=timeout_seconds)
    except ReadinessError as exc:
        print(f"ERROR: {exc}")
        return 1
    print(url)
    return 0


def wait_cmd(
    instance: str = typer.Option(..., "--instance", help="Instance whose App should be running."),
    target: str = typer.Option("dev", "--target"),
    timeout_seconds: float = typer.Option(900, "--timeout-seconds", help="Give up after this many seconds."),
) -> None:
    """Block until the deployed App is running (use between deploy and live tests)."""
    raise typer.Exit(code=_run_wait(instance=instance, target=target, timeout_seconds=timeout_seconds))


def _run_smoke(*, instance: str, target: str, timeout_seconds: float) -> int:
    """Probe the running App over its public protocols; one line per probe."""
    from agent_foundry.deploy.payload_adapters import get_payload_adapter, load_payload_config
    from agent_foundry.loader import FoundryConfigError, load_instance_config
    from agent_foundry.smoke import (
        SmokeConfigError,
        SmokeTarget,
        format_result,
        load_smoke_questions,
        normalize_host,
        probes_for,
        run_probes,
    )
    from agent_foundry.testing.readiness import app_origin
    from agent_foundry.workspace_client import get_workspace_client, load_target_auth_variables

    cwd = Path.cwd()
    instance_dir = cwd / "instances" / instance
    try:
        cfg = load_instance_config(cwd, instance=instance, target_name=target)
        adapter = get_payload_adapter(load_payload_config(instance_dir).adapter)
        questions = load_smoke_questions(instance_dir)
    except (FoundryConfigError, SmokeConfigError, ValueError, OSError) as exc:
        print(f"ERROR: could not load instance {instance!r} for target {target!r}: {exc}")
        return 1
    ws = get_workspace_client(load_target_auth_variables(cwd, target))
    app_url = ws.apps.get(cfg.app_resource_name).url or ""
    if not app_url:
        print(f"ERROR: app {cfg.app_resource_name!r} has no URL; run `agent-foundry wait` first")
        return 1
    smoke_target = SmokeTarget(
        ws=ws,
        app_url=app_origin(app_url),
        workspace_host=normalize_host(ws.config.host or ""),
        instance_dir=instance_dir,
        questions=questions,
        timeout_seconds=timeout_seconds,
    )
    try:
        probes = probes_for(adapter, smoke_target)
    except SmokeConfigError as exc:
        print(f"ERROR: {exc}")
        return 1
    results = run_probes(probes)
    for result in results:
        print(format_result(result))
    failed = sum(not result.ok for result in results)
    print(f"{len(results) - failed}/{len(results)} smoke probes passed")
    return 1 if failed else 0


def smoke_cmd(
    instance: str = typer.Option(..., "--instance", help="Instance whose deployed App is probed."),
    target: str = typer.Option("dev", "--target"),
    timeout_seconds: float = typer.Option(300, "--timeout-seconds", help="Per-request timeout."),
) -> None:
    """Call the deployed App over /invocations, MCP and A2A; exit 1 if any probe fails."""
    raise typer.Exit(code=_run_smoke(instance=instance, target=target, timeout_seconds=timeout_seconds))


def register(app: typer.Typer) -> None:
    """Add this module's commands to the root *app*."""
    app.command("test")(test_cmd)
    app.command("wait")(wait_cmd)
    app.command("smoke")(smoke_cmd)
