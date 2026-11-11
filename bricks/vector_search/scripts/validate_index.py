"""
Vector Search Index Validator
==============================
Offline validation of an index_config.yml before deployment.
Checks required fields, type consistency, and variable resolution.

Usage:
    python bricks/vector_search/scripts/validate_index.py --instance <name>
    python bricks/vector_search/scripts/validate_index.py --config path/to/index_config.yml
"""

from __future__ import annotations

import argparse
import re
from functools import partial
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from agent_foundry.bricks import load_brick_variables, resolve_sub_instances
from agent_foundry.contracts.vector_search import IndexConfig
from agent_foundry.loader import FoundryConfigError
from agent_foundry.variables import add_project_root_arg, substitute_obj

UNRESOLVED_VAR_RE = re.compile(r"\$\{var\.\w+\}")

# Lenient substitution: leaves unresolved ${var.*} in place so the validator
# can report them via _check_unresolved_vars instead of raising immediately.
substitute_variables = partial(substitute_obj, strict=False)


def load_config(config_path: Path) -> dict[str, Any]:
    """Load and parse the index_config.yml."""
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def validate_index_config(
    config: dict[str, Any],
    *,
    source: Path | str | None = None,
) -> IndexConfig:
    """Validate a (substituted) index_config.yml against :class:`IndexConfig`.

    Shape validation is delegated entirely to the typed contract -- enums,
    required fields, ``delta_sync``/``direct_access`` exclusivity, and the
    embedding source-vs-vector exclusivity are all enforced there.

    Two checks layer on top of Pydantic:

    1. Any unresolved ``${var.*}`` left after substitution is reported
       first (Pydantic would happily accept it as a plain string).
    2. ``pydantic.ValidationError`` is wrapped in a file-pointing
       :class:`~agent_foundry.loader.FoundryConfigError` so deploy logs name
       the offending file.

    Returns the parsed :class:`IndexConfig` on success; raises
    ``FoundryConfigError`` otherwise.
    """
    where = f" ({source})" if source else ""

    unresolved = _find_unresolved_vars(config)
    if unresolved:
        joined = ", ".join(sorted(set(unresolved)))
        raise FoundryConfigError(f"Unresolved variable(s) in index_config.yml{where}: {joined}")

    try:
        return IndexConfig.model_validate(config)
    except ValidationError as exc:
        raise FoundryConfigError(f"Invalid index_config.yml{where}: {exc}") from exc


def _find_unresolved_vars(config: Any) -> list[str]:
    """Walk the config tree and collect any ${var.*} left after substitution."""
    found: list[str] = []
    if isinstance(config, dict):
        for v in config.values():
            found.extend(_find_unresolved_vars(v))
    elif isinstance(config, list):
        for item in config:
            found.extend(_find_unresolved_vars(item))
    elif isinstance(config, str):
        found.extend(UNRESOLVED_VAR_RE.findall(config))
    return found


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a Vector Search index config")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--instance", help="Instance path or name (resolved under instances/<app>/vector_indexes/)")
    group.add_argument("--config", help="Direct path to index_config.yml")
    parser.add_argument("--target", default="dev", help="Deployment target")
    parser.add_argument("--vars", nargs="*", default=[], help="Variables as key=value")
    add_project_root_arg(parser)
    args = parser.parse_args(argv)

    root = args.project_root

    if args.config:
        config_path = Path(args.config)
        # instances/<app>/vector_indexes/<index>/index_config.yml -> instances/<app>
        instance_dir = config_path.resolve().parents[2]
    else:
        try:
            instance_dir, index_dirs = resolve_sub_instances(root, args.instance, "vector_indexes")
        except ValueError as exc:
            print(f"ERROR: {exc}")
            return 1
        if len(index_dirs) > 1:
            names = ", ".join(d.name for d in index_dirs)
            print(f"ERROR: --instance matched several indexes ({names}); pass '<app>/<index>'")
            return 1
        config_path = index_dirs[0] / "index_config.yml"

    if not config_path.exists():
        print(f"ERROR: Config not found: {config_path}")
        return 1

    variables = load_brick_variables(root, args.target, instance_dir, extra_vars=args.vars)

    config = load_config(config_path)
    config = substitute_variables(config, variables)

    try:
        validate_index_config(config, source=config_path)
    except FoundryConfigError as exc:
        print(f"Validation FAILED: {exc}")
        return 1

    print(f"Validation OK: {config_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
