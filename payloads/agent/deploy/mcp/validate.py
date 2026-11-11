"""
MCP brick validator (issue #107)
=================================

Pure-functional structural checks for ``server.yml`` files. The heavy
lifting (per-type required fields, ``tools.allow`` non-empty,
``cache: per_process`` rejection, ``name_prefix`` regex) lives in the
Pydantic models in :mod:`agent_foundry.contracts.mcp`. This script adds
the cross-cutting checks that depend on the on-disk YAML or runtime
variable state, namely:

- File exists and is parseable as YAML
- Pydantic validation passes
- All ``${var.*}`` placeholders resolve against the supplied variables
- Soft warning when ``prompts.enabled: true`` but ``prompts.allow`` is empty

Returns a ``(errors, warnings)`` tuple so callers can render both
streams. Both are lists of human-readable strings.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml
from pydantic import TypeAdapter, ValidationError

from agent_foundry.contracts.mcp import ServerConfig
from agent_foundry.variables import substitute_obj

# ``ServerConfig`` is an ``Annotated[Union[...], Field(discriminator=...)]``,
# not a class -- ``TypeAdapter`` is the documented way to validate
# discriminated unions in Pydantic v2. Built once at import time;
# adapters are stateless and safe to share.
_SERVER_CONFIG_ADAPTER: TypeAdapter[ServerConfig] = TypeAdapter(ServerConfig)


def _format_validation_errors(exc: ValidationError) -> list[str]:
    """Render Pydantic ``ValidationError`` as one error string per issue."""
    out: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ())) or "<root>"
        msg = err.get("msg", "validation error")
        out.append(f"{loc}: {msg}")
    return out


def _load_yaml(yml_path: Path) -> tuple[dict[str, Any] | None, list[str]]:
    """Load and parse YAML, returning (data, errors)."""
    if not yml_path.exists():
        return None, [f"server.yml not found at {yml_path}"]
    try:
        raw = yaml.safe_load(yml_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        return None, [f"server.yml is not valid YAML: {exc}"]
    if not isinstance(raw, dict):
        return None, [f"server.yml must be a mapping at the top level, got {type(raw).__name__}"]
    return raw, []


def _validate_pydantic(
    raw: dict[str, Any],
    variables: dict[str, str],
) -> tuple[ServerConfig | None, list[str]]:
    """Substitute vars and validate the Pydantic shape.

    Variable substitution happens *before* Pydantic validation so that
    type discriminators and required fields can be string templates.
    Unresolved ``${var.*}`` references raise ``ValueError`` from
    :func:`substitute_obj`, which we report as a structured error.
    """
    try:
        resolved = substitute_obj(raw, variables, strict=True)
    except ValueError as exc:
        return None, [f"unresolved variable: {exc}"]

    try:
        config = _SERVER_CONFIG_ADAPTER.validate_python(resolved)
    except ValidationError as exc:
        return None, _format_validation_errors(exc)
    except NotImplementedError as exc:
        return None, [str(exc)]

    return config, []


def _collect_warnings(config: ServerConfig) -> list[str]:
    """Soft warnings -- not blocking, but usually a config mistake."""
    warnings: list[str] = []
    if config.prompts.enabled and not config.prompts.allow:
        warnings.append(
            "prompts.enabled is true but prompts.allow is empty -- "
            "no prompts will be consumed (set prompts.allow to opt in)"
        )
    if config.resources.enabled and not config.resources.allow:
        warnings.append("resources.enabled is true but resources.allow is empty -- no resources will be exposed")
    # ``resolved_auto_poll`` is the post-validator view: ``AutoPollConfig
    # | None``.  Reading the raw ``auto_poll`` field would carry the
    # ``bool`` shape from the YAML contract and force ``union-attr``
    # ignores at every access; the property exists exactly for this.
    auto_poll = config.resolved_auto_poll
    if auto_poll is not None:
        # The wrapper looks up the trigger / poll_with by alias against
        # the post-aliasing tools.allow surface.  When either is missing
        # the runtime gracefully falls through to no-op (logged), but
        # the deploy is the right place to nudge the user.
        aliases = _allow_aliases(config)
        if auto_poll.trigger not in aliases:
            warnings.append(
                f"auto_poll.trigger {auto_poll.trigger!r} is not "
                "in tools.allow (after expose_as) -- the wrapper will "
                "never invoke the auto-poll loop; either add the "
                "trigger tool to tools.allow or set auto_poll: false"
            )
        if auto_poll.poll_with not in aliases:
            warnings.append(
                f"auto_poll.poll_with {auto_poll.poll_with!r} is "
                "not in tools.allow (after expose_as) -- the wrapper "
                "cannot resolve the poll tool's upstream name; auto-poll "
                "will fall back to returning the unpolled response"
            )
    return warnings


def _allow_aliases(config: ServerConfig) -> set[str]:
    """Set of LLM-visible alias names from ``tools.allow``.

    Bare strings collapse to themselves; ``ToolAllowEntry`` mappings
    use ``expose_as`` when present, otherwise the ``match`` literal.
    Mirrors :class:`payloads.agent.runtime.mcp_wrapper.FilteredMCPServerStreamableHttp._resolve_alias`
    so the validator and runtime stay in sync.
    """
    aliases: set[str] = set()
    for entry in config.tools.allow:
        if isinstance(entry, str):
            aliases.add(entry)
        else:
            aliases.add(entry.expose_as or entry.match)
    return aliases


def validate_server_yml(
    yml_path: Path,
    variables: dict[str, str],
) -> tuple[ServerConfig | None, list[str], list[str]]:
    """Validate one ``server.yml`` end-to-end.

    Returns ``(config, errors, warnings)``. ``config`` is ``None`` when
    *errors* is non-empty; callers should not proceed to deploy when
    that's the case.
    """
    raw, errors = _load_yaml(yml_path)
    if raw is None:
        return None, errors, []

    config, errors = _validate_pydantic(raw, variables)
    if config is None:
        return None, errors, []

    return config, [], _collect_warnings(config)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate an MCP server.yml file (offline, no network).",
    )
    parser.add_argument(
        "server_yml",
        type=Path,
        help="Path to the server.yml file to validate.",
    )
    parser.add_argument(
        "--vars",
        nargs="*",
        default=[],
        help="Extra variables as key=value pairs (for ${var.*} resolution).",
    )
    args = parser.parse_args(argv)

    variables: dict[str, str] = {}
    for kv in args.vars:
        if "=" in kv:
            k, v = kv.split("=", 1)
            variables[k] = v

    _, errors, warnings = validate_server_yml(args.server_yml, variables)

    for w in warnings:
        print(f"  WARNING: {w}")

    if errors:
        for e in errors:
            print(f"  ERROR: {e}", file=sys.stderr)
        return 1

    print(f"  OK: {args.server_yml} validated successfully")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
