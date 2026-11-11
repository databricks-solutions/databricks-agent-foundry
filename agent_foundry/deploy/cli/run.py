"""``agent-foundry run`` and ``agent-foundry repl`` -- local agent dev loop."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import typer


def run_cmd(
    instance: str = typer.Option(..., "--instance", help="Instance name."),
    port: int = typer.Option(8000, "--port", help="Backend port (default 8000)."),
    no_ui: bool = typer.Option(
        False,
        "--no-ui",
        help="Run backend only, skip the bundled chat UI.",
    ),
    reload: bool = typer.Option(
        False,
        "--reload",
        help="Enable uvicorn auto-reload (dev).",
    ),
) -> None:
    """Start the local AgentServer (and chat UI by default)."""
    from agent_foundry import env as af_env

    subprocess_env = {
        **dict(os.environ),
        af_env.AGENT_FOUNDRY_PROJECT_ROOT.name: str(Path.cwd().resolve()),
        af_env.INSTANCE_DIR.name: f"instances/{instance}",
    }
    # Invoke via ``python -m`` so wheel-installed downstream repos (no
    # ``bricks/app/scripts/start_app.py`` on disk relative to CWD)
    # can run ``agent-foundry run`` -- issue #112.
    cmd = [
        sys.executable,
        "-m",
        "bricks.app.scripts.start_app",
        "--port",
        str(port),
    ]
    if no_ui:
        cmd.append("--no-ui")
    if reload:
        cmd.append("--reload")
    raise typer.Exit(code=subprocess.run(cmd, env=subprocess_env).returncode)


def repl_cmd(
    instance: str = typer.Option(..., "--instance", help="Instance name."),
) -> None:
    """Open an interactive REPL against the agent in this instance."""
    from agent_foundry import env as af_env

    subprocess_env = {
        **dict(os.environ),
        af_env.AGENT_FOUNDRY_PROJECT_ROOT.name: str(Path.cwd().resolve()),
        af_env.INSTANCE_DIR.name: f"instances/{instance}",
    }
    raise typer.Exit(
        code=subprocess.run(
            [sys.executable, "-m", "payloads.agent.runtime.agent"],
            env=subprocess_env,
        ).returncode
    )


def register(app: typer.Typer) -> None:
    """Add this module's commands to the root *app*."""
    app.command("run")(run_cmd)
    app.command("repl")(repl_cmd)
