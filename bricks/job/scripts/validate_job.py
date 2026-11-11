"""Offline validation for ``jobs/<name>/job.yml``."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from agent_foundry.bricks import load_brick_variables, resolve_sub_instances
from agent_foundry.contracts.jobs import JobConfig
from agent_foundry.loader import FoundryConfigError
from agent_foundry.variables import add_project_root_arg, substitute_obj

UNRESOLVED_VAR_RE = re.compile(r"\$\{var\.[^}]+\}")
# ``${var.vs_index_name__<index>}`` exists only after the Vector Search brick runs.
VS_INDEX_OUTPUT_PREFIX = "${var.vs_index_name__"


def _find_unresolved_vars(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for child in value.values():
            found.extend(_find_unresolved_vars(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_find_unresolved_vars(child))
    elif isinstance(value, str):
        found.extend(UNRESOLVED_VAR_RE.findall(value))
    return found


def validate_job_config(
    config: dict[str, Any],
    *,
    source: Path | str | None = None,
) -> JobConfig:
    where = f" ({source})" if source else ""
    unresolved = sorted(set(_find_unresolved_vars(config)))
    if unresolved:
        raise FoundryConfigError(f"Unresolved variable(s) in job.yml{where}: {', '.join(unresolved)}")
    try:
        return JobConfig.model_validate(config)
    except ValidationError as exc:
        raise FoundryConfigError(f"Invalid job.yml{where}: {exc}") from exc


def only_vs_index_outputs_missing(exc: FoundryConfigError) -> bool:
    """True when the only unresolved variables are Vector Search index outputs."""
    message = str(exc)
    if "Unresolved variable" not in message:
        return False
    names = UNRESOLVED_VAR_RE.findall(message)
    return bool(names) and all(name.startswith(VS_INDEX_OUTPUT_PREFIX) for name in names)


def load_job_config(job_dir: Path, variables: dict[str, str]) -> JobConfig:
    config_path = job_dir / "job.yml"
    if not config_path.exists():
        raise FileNotFoundError(f"Job config not found: {config_path}")
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    substituted = substitute_obj(raw, variables, strict=False)
    return validate_job_config(substituted, source=config_path)


def _validate_one(job_dir: Path, variables: dict[str, str]) -> bool:
    """Validate one job and print the outcome; ``True`` when it failed."""
    try:
        load_job_config(job_dir, variables)
    except FoundryConfigError as exc:
        if only_vs_index_outputs_missing(exc):
            print(f"Validation SKIPPED (Vector Search index not deployed yet): {exc}")
            return False
        print(f"Validation FAILED: {exc}")
        return True
    except OSError as exc:
        print(f"Validation FAILED: {exc}")
        return True
    print(f"Validation OK: {job_dir / 'job.yml'}")
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a Databricks Job config")
    parser.add_argument(
        "--instance",
        required=True,
        help="App name (validates every job), '<app>/<job>', '<app>/jobs/<job>', or a path to either",
    )
    parser.add_argument("--target", default="dev")
    parser.add_argument("--vars", nargs="*", default=[])
    add_project_root_arg(parser)
    args = parser.parse_args(argv)

    try:
        instance_dir, job_dirs = resolve_sub_instances(args.project_root, args.instance, "jobs")
    except ValueError as exc:
        print(f"Validation FAILED: {exc}")
        return 1
    variables = load_brick_variables(
        args.project_root,
        args.target,
        instance_dir,
        extra_vars=args.vars,
    )
    failures = sum(_validate_one(job_dir, variables) for job_dir in job_dirs)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
