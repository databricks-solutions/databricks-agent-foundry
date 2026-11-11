"""
MCP server inspection CLI (issue #107)
=======================================

``agent-foundry brick mcp inspect <name> --instance <app> --target dev``
walks the deploy-time ``server.yml`` plus a live ``list_*`` round-trip
against the configured MCP server and prints a human-readable summary
of tools / prompts / resources, marking entries already present in the
respective ``allow`` lists.

This is a *deployer* utility -- it does not run as part of any deploy
or runtime path. The network calls are gated behind the optional
``[call]`` extra (``databricks-mcp``); without it the command exits
with a friendly install hint.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_foundry._instance_layout import resolve_payload_subdir
from agent_foundry.bricks import fill_state_outputs, load_brick_variables
from agent_foundry.contracts.mcp import ServerConfig
from agent_foundry.variables import add_project_root_arg
from agent_foundry.workspace_client import prefer_workspace_client
from payloads.agent.deploy.mcp.deploy import build_canonical_url
from payloads.agent.deploy.mcp.validate import validate_server_yml

# ---------------------------------------------------------------------------
# Result containers (test-friendly: pure data, no live state)
# ---------------------------------------------------------------------------


@dataclass
class Discovery:
    """Live ``list_*`` results from one MCP server.

    Each list contains the raw protocol objects (``mcp.types.Tool``
    / ``mcp.types.Prompt`` / ``mcp.types.Resource``) so the renderer can
    pick out only the fields it needs (name, description, annotations).
    """

    tools: list[Any] = field(default_factory=list)
    prompts: list[Any] = field(default_factory=list)
    resources: list[Any] = field(default_factory=list)
    # Per-primitive error message captured when the corresponding
    # ``list_*`` call fails (e.g. server doesn't implement prompts).
    # Keys: "tools", "prompts", "resources".
    errors: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Path / config resolution
# ---------------------------------------------------------------------------


def _resolve_server_dir(instance: str, name: str, project_root: Path) -> Path:
    """Map ``--instance demo --name github`` to a sub-directory."""
    instance_dir = project_root / "instances" / instance
    return resolve_payload_subdir(instance_dir, "mcp_servers") / name


def _server_kind_summary(config: ServerConfig) -> str:
    """Render the ``(type via fields)`` parenthetical for the header."""
    # Local imports keep the small isinstance ladder self-contained.
    from agent_foundry.contracts.mcp import (
        CustomAppServerConfig,
        ExternalServerConfig,
        ManagedFunctionsServerConfig,
        ManagedGenieServerConfig,
        ManagedSqlServerConfig,
        ManagedVectorSearchServerConfig,
    )

    if isinstance(config, ManagedFunctionsServerConfig):
        return f"managed_functions via {config.catalog}.{config.schema_}"
    if isinstance(config, ManagedGenieServerConfig):
        return f"managed_genie via space {config.space_id}"
    if isinstance(config, ManagedVectorSearchServerConfig):
        return f"managed_vector_search via {config.catalog}.{config.schema_}"
    if isinstance(config, ManagedSqlServerConfig):
        return "managed_sql"
    if isinstance(config, ExternalServerConfig):
        return f"external via {config.connection_name}"
    if isinstance(config, CustomAppServerConfig):
        return f"custom_app at {config.app_url}"
    return config.type


# ---------------------------------------------------------------------------
# Live discovery (gated behind the [call] extra)
# ---------------------------------------------------------------------------


def _missing_extra(extra: str, exc: Exception) -> ImportError:
    return ImportError(
        f"agent-foundry's {extra!r} extra is required for 'brick mcp inspect'. "
        f"Install with: uv pip install 'agent-foundry[call]'  "
        f"(original error: {exc})"
    )


def _list_tools(url: str, ws: Any) -> tuple[list[Any], str | None]:
    """List tools via ``DatabricksMCPClient``; returns (tools, error)."""
    try:
        from databricks_mcp import DatabricksMCPClient
    except ImportError as exc:  # pragma: no cover - import error path
        raise _missing_extra("call", exc) from exc
    try:
        client = DatabricksMCPClient(server_url=url, workspace_client=ws)
        return list(client.list_tools()), None
    except Exception as exc:
        return [], f"{type(exc).__name__}: {exc}"


async def _list_prompts_resources_async(
    url: str,
    ws: Any,
) -> tuple[list[Any], str | None, list[Any], str | None]:
    """Fetch prompts and resources via the raw ``mcp`` ClientSession.

    ``DatabricksMCPClient`` (as of 0.3.x) only wraps tools, so prompts
    and resources go through the lower-level streamable-HTTP transport
    using the same OAuth provider.
    """
    try:
        from databricks_mcp.oauth_provider import DatabricksOAuthClientProvider
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client
    except ImportError as exc:  # pragma: no cover - import error path
        raise _missing_extra("call", exc) from exc

    auth = DatabricksOAuthClientProvider(ws)
    prompts: list[Any] = []
    prompts_err: str | None = None
    resources: list[Any] = []
    resources_err: str | None = None

    async with streamablehttp_client(url, auth=auth) as (read, write, _get_session_id):
        async with ClientSession(read, write) as session:
            await session.initialize()
            try:
                prompts_resp = await session.list_prompts()
                prompts = list(prompts_resp.prompts)
            except Exception as exc:
                prompts_err = f"{type(exc).__name__}: {exc}"
            try:
                resources_resp = await session.list_resources()
                resources = list(resources_resp.resources)
            except Exception as exc:
                resources_err = f"{type(exc).__name__}: {exc}"

    return prompts, prompts_err, resources, resources_err


def discover(url: str, ws: Any) -> Discovery:
    """Fetch the live tools / prompts / resources lists from ``url``."""
    out = Discovery()
    out.tools, tools_err = _list_tools(url, ws)
    if tools_err:
        out.errors["tools"] = tools_err

    try:
        prompts, prompts_err, resources, resources_err = asyncio.run(_list_prompts_resources_async(url, ws))
        out.prompts = prompts
        out.resources = resources
        if prompts_err:
            out.errors["prompts"] = prompts_err
        if resources_err:
            out.errors["resources"] = resources_err
    except Exception as exc:
        # Catch transport-level failures so a missing endpoint doesn't
        # blank out the tools section we already fetched successfully.
        out.errors.setdefault("prompts", f"{type(exc).__name__}: {exc}")
        out.errors.setdefault("resources", f"{type(exc).__name__}: {exc}")

    return out


# ---------------------------------------------------------------------------
# Rendering (pure: takes data, returns string -- easy to test)
# ---------------------------------------------------------------------------


def _annotation_tags(tool: Any) -> str:
    """Render MCP tool ``annotations`` as ``[hintName, ...]`` or empty."""
    annotations = getattr(tool, "annotations", None)
    if annotations is None:
        return ""
    hints = []
    for hint in (
        "readOnlyHint",
        "destructiveHint",
        "idempotentHint",
        "openWorldHint",
    ):
        if getattr(annotations, hint, None) is True:
            hints.append(hint)
    return f"  [{', '.join(hints)}]" if hints else ""


def _truncate(text: str | None, width: int = 60) -> str:
    """Single-line truncate so the inspect output stays scannable."""
    if not text:
        return ""
    one_line = " ".join(text.split())
    if len(one_line) <= width:
        return one_line
    return one_line[: width - 3] + "..."


def _match_allow_entry(
    upstream_name: str,
    allow: list[Any],
) -> tuple[bool, str | None]:
    """Return ``(matched, alias_or_None)`` for one upstream tool.

    Walks ``allow`` in order, treating each entry as either a bare
    string (exact match, no rename) or a :class:`ToolAllowEntry`
    (fnmatch glob + optional ``expose_as`` rename).  Returns the first
    match's alias (or ``None`` when no rename is requested), so the
    inspect output can show ``upstream -> alias`` for the user.
    """
    import fnmatch as _fnmatch

    from agent_foundry.contracts.mcp import ToolAllowEntry

    for entry in allow:
        if isinstance(entry, str):
            if entry == upstream_name:
                return True, None
        elif isinstance(entry, ToolAllowEntry):
            if _fnmatch.fnmatchcase(upstream_name, entry.match):
                return True, entry.expose_as
    return False, None


def render(
    server_name: str,
    config: ServerConfig,
    url: str,
    discovery: Discovery,
) -> str:
    """Format a discovery result as the inspect CLI's stdout text.

    Uses ASCII ``[x]`` / ``[ ]`` glyphs (per the issue spec) so the
    output renders in any terminal and copy-pastes cleanly into PR
    descriptions.
    """
    lines: list[str] = []
    lines.append(f"Server: {server_name} ({_server_kind_summary(config)})")
    lines.append(f"URL:    {url}")

    # --- Tools ---
    matches = [_match_allow_entry(t.name, list(config.tools.allow)) for t in discovery.tools]
    n_tools = len(discovery.tools)
    n_allowed = sum(1 for matched, _ in matches if matched)
    lines.append("")
    if "tools" in discovery.errors:
        lines.append(f"Tools (ERROR: {discovery.errors['tools']}):")
    else:
        lines.append(f"Tools ({n_tools} discovered, {n_allowed} in tools.allow):")
        if not discovery.tools:
            lines.append("  (none)")
        for tool, (matched, alias) in zip(discovery.tools, matches):
            mark = "[x]" if matched else "[ ]"
            # When the entry sets ``expose_as`` show "upstream -> alias"
            # so the deployer can see exactly what name the LLM will
            # call (after the server's name_prefix is concatenated).
            shown = f"{tool.name} -> {alias}" if matched and alias is not None else tool.name
            desc = _truncate(getattr(tool, "description", ""))
            lines.append(f"  {mark} {shown:<24}{desc}{_annotation_tags(tool)}")

    # --- Prompts ---
    allow_prompts = set(config.prompts.allow)
    n_prompts = len(discovery.prompts)
    enabled = config.prompts.enabled
    lines.append("")
    if "prompts" in discovery.errors:
        lines.append(f"Prompts (ERROR: {discovery.errors['prompts']}):")
    else:
        suffix = "" if enabled else f", prompts.enabled={enabled}".lower()
        lines.append(f"Prompts ({n_prompts} discovered{suffix}):")
        if not discovery.prompts:
            lines.append("  (none)")
        elif not enabled:
            lines.append("  (none consumed; set prompts.enabled: true in server.yml to use)")
        for prompt in discovery.prompts:
            mark = "[x]" if (prompt.name in allow_prompts or "*" in allow_prompts) else "[ ]"
            desc = _truncate(getattr(prompt, "description", ""))
            lines.append(f"  {mark} {prompt.name:<28}{desc}")

    # --- Resources ---
    n_resources = len(discovery.resources)
    enabled_r = config.resources.enabled
    lines.append("")
    if "resources" in discovery.errors:
        lines.append(f"Resources (ERROR: {discovery.errors['resources']}):")
    else:
        suffix = "" if enabled_r else f", resources.enabled={enabled_r}".lower()
        lines.append(f"Resources ({n_resources} discovered{suffix}):")
        if not discovery.resources:
            lines.append("  (none)")
        elif not enabled_r:
            lines.append("  (none exposed; set resources.enabled: true in server.yml to use)")
        for resource in discovery.resources:
            uri = getattr(resource, "uri", "?")
            desc = _truncate(getattr(resource, "description", ""))
            lines.append(f"  - {uri}  {desc}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Inspect an MCP server's discovered tools / prompts / resources. "
            "Marks entries already present in the corresponding allow-list "
            "with [x]."
        ),
    )
    parser.add_argument(
        "name",
        help="Server sub-directory name (under instances/<instance>/mcp_servers/).",
    )
    parser.add_argument(
        "--instance",
        required=True,
        help="Instance name (resolved under instances/<name>/).",
    )
    parser.add_argument("--target", default="dev")
    parser.add_argument(
        "--vars",
        nargs="*",
        default=[],
        help="Extra variables as key=value pairs.",
    )
    parser.add_argument(
        "--profile",
        default=None,
        help="Databricks CLI profile (defaults to SDK credential resolution).",
    )
    add_project_root_arg(parser)
    args = parser.parse_args(argv)

    project_root = args.project_root
    sub_dir = _resolve_server_dir(args.instance, args.name, project_root)
    yml_path = sub_dir / "server.yml"
    if not yml_path.exists():
        print(f"Error: {yml_path} not found", file=sys.stderr)
        return 1

    ws = prefer_workspace_client(profile=args.profile)
    if ws is None:
        print("Error: no Databricks workspace client available.", file=sys.stderr)
        return 1

    instance_dir = project_root / "instances" / args.instance
    variables = load_brick_variables(project_root, args.target, instance_dir, extra_vars=args.vars)
    variables = fill_state_outputs(variables, instance_dir, args.target, ws)

    config, errors, warnings = validate_server_yml(yml_path, variables)
    for w in warnings:
        print(f"  WARNING: {w}")
    if config is None:
        for e in errors:
            print(f"  ERROR: {e}", file=sys.stderr)
        return 1

    try:
        url = build_canonical_url(config, variables.get("host", ""))
    except ValueError as exc:
        print(f"  ERROR: {exc}", file=sys.stderr)
        return 1

    try:
        discovery = discover(url, ws)
    except ImportError as exc:
        print(f"  ERROR: {exc}", file=sys.stderr)
        return 1

    print(render(args.name, config, url, discovery))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
