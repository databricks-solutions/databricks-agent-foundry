"""Acceptance orchestration a real consumer does not own: scenario matrix and scripted init answers."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, get_args

from agent_foundry.testing.fixtures import apply_fixture, fixture_dir
from agent_foundry.testing.identity import make_run_identity
from agent_foundry.testing.scenarios import Kind, Scenario, load_scenarios
from agent_foundry.testing.workspace import load_workspace_binding


def wizard_stdin(scenario: Scenario, *, project_name: str, host: str, catalog: str) -> str:
    """Scripted answers for packaged ``agent-foundry init`` (dev target only)."""
    return (
        "\n".join(
            [
                project_name,
                host,
                catalog,
                "",
                "n",
                "n",
                "git",
                scenario.variant_id,
            ]
        )
        + "\n"
    )


def _cmd_matrix(args: argparse.Namespace) -> int:
    rows = [
        {
            "id": s.id,
            "kind": s.kind,
            "variant_id": s.variant_id,
            "lane": s.lane,
            "timeout_minutes": s.timeout_minutes,
        }
        for s in load_scenarios()
        if args.kind is None or s.kind == args.kind
    ]
    json.dump(rows, sys.stdout)
    sys.stdout.write("\n")
    return 0


def _cmd_prepare(args: argparse.Namespace) -> int:
    scenario = _scenario(args.scenario)
    identity = make_run_identity(scenario, args.run_id)
    binding = load_workspace_binding()
    payload = {
        "project_name": identity.project_name,
        "schema_prefix": identity.schema_prefix,
        "app_name": identity.app_name,
        "run_token": identity.run_token,
        "kind": scenario.kind,
        "variant_id": scenario.variant_id,
        "scaffolds": list(scenario.scaffolds),
        "checks": list(scenario.checks),
        "lane": scenario.lane,
        "timeout_minutes": scenario.timeout_minutes,
        "host": binding.host,
        "catalog": binding.catalog,
        "target": binding.target,
        "stdin": wizard_stdin(
            scenario,
            project_name=identity.project_name,
            host=binding.host,
            catalog=binding.catalog,
        ),
    }
    json.dump(payload, sys.stdout)
    sys.stdout.write("\n")
    return 0


def _cmd_apply_fixture(args: argparse.Namespace) -> int:
    scenario = _scenario(args.scenario)
    if not scenario.fixture:
        return 0
    for rel in apply_fixture(fixture_dir(scenario.id), args.instance_dir):
        print(f"added {rel}")
    return 0


def _scenario(scenario_id: str) -> Scenario:
    match = [s for s in load_scenarios() if s.id == scenario_id]
    if not match:
        raise SystemExit(f"unknown scenario {scenario_id!r}")
    return match[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agent_foundry.testing.harness")
    sub = parser.add_subparsers(dest="command", required=True)

    matrix = sub.add_parser("matrix", help="Print the scenario catalog as JSON")
    matrix.add_argument("--kind", choices=get_args(Kind), help="Only scenarios of this level")

    prepare = sub.add_parser("prepare", help="Validate env and print run identity + init stdin")
    prepare.add_argument("--scenario", required=True)
    prepare.add_argument("--run-id")

    fixture = sub.add_parser(
        "apply-fixture", help="Add the scenario's fixture files to an instance (no-op without one)"
    )
    fixture.add_argument("--scenario", required=True)
    fixture.add_argument("--instance-dir", required=True, type=Path)

    args = parser.parse_args(argv)
    handlers: dict[str, Any] = {
        "matrix": _cmd_matrix,
        "prepare": _cmd_prepare,
        "apply-fixture": _cmd_apply_fixture,
    }
    return int(handlers[args.command](args))


if __name__ == "__main__":
    raise SystemExit(main())
