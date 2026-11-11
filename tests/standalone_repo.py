"""Minimal synthetic downstream repo used by unit and component CLI smokes."""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

SYNTHETIC_INSTANCE_NAME = "smoke-test-app"


def seed_standalone_repo(dest: Path) -> None:
    """Populate *dest* with a repo-root + per-instance foundry.toml, no bricks."""
    instance_dir = dest / "instances" / SYNTHETIC_INSTANCE_NAME
    instance_dir.mkdir(parents=True)
    (dest / "foundry.toml").write_text(
        textwrap.dedent(f"""\
        schema_version = 2

        [targets.dev]
        host    = "https://dry-run.example.com"
        profile = "dry-run"
        catalog = "main"
        schema  = "analytics"

        [[instances]]
        path = "instances/{SYNTHETIC_INSTANCE_NAME}"
    """)
    )
    (instance_dir / "foundry.toml").write_text(
        textwrap.dedent(f"""\
        name          = "{SYNTHETIC_INSTANCE_NAME}"
        description   = "Minimal synthetic instance for the wheel-install smoke test."
        schema_prefix = "smoke_test_app"

        [warehouse]
        mode         = "reuse"
        warehouse_id = "dry-run-warehouse"
    """)
    )
    payload_dir = instance_dir / "payloads" / "agent"
    payload_dir.mkdir(parents=True)
    (payload_dir / "payload.yml").write_text(
        textwrap.dedent("""\
        payload:
          adapter: agent
          requires: []
          spec: {}
        """)
    )


def packaged_cli_env() -> dict[str, str]:
    env = {
        **os.environ,
        "AGENT_FOUNDRY_TELEMETRY_DISABLED": "true",
        "NO_COLOR": "1",
    }
    env.pop("AGENT_FOUNDRY_SKIP_DEFAULT_WORKSPACE_CLIENT", None)
    return env


def run_packaged_cli(
    script: Path,
    args: list[str],
    *,
    cwd: Path,
    timeout: int = 120,
    input: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(script), *args],
        cwd=str(cwd),
        env=packaged_cli_env(),
        capture_output=True,
        text=True,
        timeout=timeout,
        input=input,
    )
