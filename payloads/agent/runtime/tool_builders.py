"""Build the agent's tools: direct SDK tools, skill-declared tools and python tools."""

from __future__ import annotations

import logging
from typing import Any

from agents import FunctionTool
from databricks.sdk import WorkspaceClient
from pydantic import ValidationError

from agent_foundry.contracts import Tool, ToolManifest
from payloads.agent.runtime._tool_factory import create_function_tool
from payloads.agent.runtime.request_context import resolve_tools_client

logger = logging.getLogger(__name__)


_DIRECT_SKILL_TOOL_TYPES: frozenset[str] = frozenset({"genie", "vector_search"})


def warn_dropped_direct_tools(config: dict[str, Any]) -> None:
    """Log a WARNING per direct-mode tool declaration suppressed under
    ``mcp_only=True``.

    Direct-mode types served by MCP (``uc_function``, ``mcp``) are
    skipped here because the
    assembler already routes them through the MCP path -- a warning
    would be noise.  Only the two surviving direct-call types
    (``genie``, ``vector_search``) are reported, since those would
    silently disappear from the LLM-visible toolset under ``mcp_only``.
    """
    for entry in config.get("TOOLS", []) or []:
        if not isinstance(entry, dict):
            continue
        tool_type = entry.get("type", "?")
        # ``python`` tools are first-party in-process tools, not the
        # legacy direct-mode Genie/VS the ``mcp_only`` flag gates; they are
        # attached regardless (see :func:`create_python_tools`), so they
        # are never "dropped".
        if tool_type == "python":
            continue
        name = entry.get("name") or tool_type
        logger.warning(
            "[mcp_only] dropping direct-mode agent tool %r (type=%s); "
            "set 'runtime.tools.mcp_only: false' in payloads/agent/payload.yml to keep it",
            name,
            tool_type,
        )
    for entry in config.get("SKILL_TOOLS", []) or []:
        if not isinstance(entry, dict):
            continue
        tool_type = entry.get("type", "?")
        if tool_type not in _DIRECT_SKILL_TOOL_TYPES:
            continue
        logger.warning(
            "[mcp_only] dropping direct-mode skill tool %r (type=%s); "
            "re-serve via the bricks/mcp/ brick or set 'mcp_only: false'",
            entry.get("name", "?"),
            tool_type,
        )


def create_tools(
    sp_ws: WorkspaceClient,
    config: dict[str, Any],
    variables: dict[str, str],
) -> list[FunctionTool]:
    """Create FunctionTools for agent-level Genie / Vector Search tools.

    ``config["TOOLS"]`` is the ``model_dump`` of ``AgentConfig.tools_typed``
    -- a ``list[dict]`` keyed by a ``type`` discriminator (``"genie"``,
    ``"vector_search"``, ...) since 67-E / PR #80.  Presence in the list
    means enabled; there is no separate ``enabled`` flag any more.  Tool
    calls run as the user (see :func:`resolve_tools_client`).
    """
    tools: list[FunctionTool] = []
    tools_cfg = config.get("TOOLS", [])

    if not isinstance(tools_cfg, list):
        logger.warning(
            "[tools] unexpected TOOLS shape %s (expected list[dict]); "
            "skipping agent-level tools. Run the 67-E migration.",
            type(tools_cfg).__name__,
        )
        return tools
    if not tools_cfg:
        return tools

    ws = resolve_tools_client(sp_ws)

    for tool_cfg in tools_cfg:
        if not isinstance(tool_cfg, dict):
            logger.warning("[tools] skipping non-dict tool entry: %r", tool_cfg)
            continue

        # Agent-level TOOLS predates the typed contracts (#67-E) and is
        # tolerant of partial dicts that omit ``name`` / ``description``.
        # Synthesize sensible defaults before typed validation so the
        # entry can flow through the shared :func:`create_function_tool`
        # dispatch.  When ``tools_typed`` is the source (the supported
        # path going forward) the defaults are no-ops because the
        # AgentConfig validator already populated both fields.
        tool = _as_tool(_with_legacy_defaults(tool_cfg))
        if tool is None:
            continue

        # Agent-level TOOLS supports a legacy variable-fallback: an entry
        # without an explicit ``space_id`` / ``index_name`` is resolved
        # against ``variables`` (typically populated from the parent
        # genie / vector_search brick state).  The skill-level path does
        # not do this -- skill tools are bound by the assembler.
        try:
            function_tool = create_function_tool(
                ws,
                tool,
                fallback_space_id=variables.get("genie_space_id", ""),
                fallback_index_name=variables.get("vs_index_name", ""),
                log_prefix="tools",
            )
        except Exception as exc:
            logger.warning(
                "[tools] %s tool unavailable -- skipping: %s",
                tool.type,
                exc,
            )
            continue
        if function_tool is not None:
            tools.append(function_tool)

    logger.info("[tools] created %d agent-level tool(s)", len(tools))
    return tools


# Default ``name`` per tool type for legacy ``TOOLS`` dicts that omit it.
# Matches the historical defaults applied by the pre-refactor
# implementation of :func:`create_tools` (which never threaded a name
# through to the factory at all, so the factories' own defaults --
# ``query_genie`` / ``query_vector_search`` -- ended up on the
# ``FunctionTool``).
_LEGACY_TOOL_NAME_DEFAULTS: dict[str, str] = {
    "genie": "query_genie",
    "vector_search": "query_vector_search",
}


def _with_legacy_defaults(tool_cfg: dict[str, Any]) -> dict[str, Any]:
    """Fill in the ``name`` / ``description`` fields a legacy
    agent-level ``TOOLS`` dict may omit.  Skill-level callers do not
    use this -- their dicts come from the assembler's typed dump and
    always have both fields.
    """
    tool_type = tool_cfg.get("type")
    if not isinstance(tool_type, str):
        return tool_cfg
    if tool_cfg.get("name") and tool_cfg.get("description"):
        return tool_cfg
    patched = dict(tool_cfg)
    patched.setdefault(
        "name",
        _LEGACY_TOOL_NAME_DEFAULTS.get(tool_type, tool_type),
    )
    patched.setdefault(
        "description",
        f"Legacy agent-level {tool_type} tool (no description supplied).",
    )
    return patched


def _as_tool(tool_cfg: dict[str, Any]) -> Tool | None:
    """Adapt a raw ``SKILL_TOOLS`` dict to the typed ``Tool`` union.

    ``SKILL_TOOLS`` is produced by ``payloads.agent.deploy.skills.assemble`` via
    ``Tool.model_dump(exclude_unset=True)`` (#67-A), so every
    assembler-produced entry round-trips cleanly. Returns ``None`` +
    warning for hand-written or malformed entries, preserving the
    runtime's "skip unknown, keep going" behaviour until the config
    boundary itself is typed (#67-E).
    """
    try:
        return ToolManifest.model_validate({"tools": [tool_cfg]}).tools[0]
    except ValidationError as exc:
        name = tool_cfg.get("name", "?")
        tool_type = tool_cfg.get("type", "?")
        logger.warning(
            "[skill_tools] invalid tool config '%s' (type=%s) — skipping: %s",
            name,
            tool_type,
            exc,
        )
        return None


# ---------------------------------------------------------------------------
# Skill tool creation (driven by skills/*/tools.yml declarations)
# ---------------------------------------------------------------------------


def create_skill_tools(
    sp_ws: WorkspaceClient,
    config: dict[str, Any],
    variables: dict[str, str],
    *,
    existing_tool_names: set[str] | None = None,
) -> list[FunctionTool]:
    """Create FunctionTools from skill tool declarations in ``SKILL_TOOLS``.

    Each entry in ``config["SKILL_TOOLS"]`` is a dict with at minimum
    ``name``, ``type``, and ``description``.  The ``type`` determines which
    factory is called.

    Tools whose name appears in *existing_tool_names* are skipped to avoid
    duplicating tools already created from ``spec.runtime.tools.definitions``.
    """
    skill_tools_cfg: list[dict[str, Any]] = config.get("SKILL_TOOLS", [])
    if not skill_tools_cfg:
        return []

    ws = resolve_tools_client(sp_ws)
    tools: list[FunctionTool] = []
    skip_names = existing_tool_names or set()

    for tool_cfg in skill_tools_cfg:
        tool = _as_tool(tool_cfg)
        if tool is None:
            continue

        if tool.name in skip_names:
            logger.info("[skill_tools] skipping '%s' — already created at agent level", tool.name)
            continue

        try:
            function_tool = _create_skill_tool(ws, tool)
            if function_tool is not None:
                tools.append(function_tool)
                skip_names.add(tool.name)
        except Exception as exc:
            logger.warning(
                "[skill_tools] failed to create '%s' (%s) — skipping: %s",
                tool.name,
                tool.type,
                exc,
            )

    logger.info("[skill_tools] created %d skill tool(s)", len(tools))
    return tools


def _create_skill_tool(
    ws: WorkspaceClient,
    tool: Tool,
) -> FunctionTool | None:
    """Thin wrapper around :func:`create_function_tool` for the skills
    code path.  No variable fallback -- skill tools are bound by the
    assembler at deploy time, so any missing required field is a real
    error and skipped with a warning.
    """
    return create_function_tool(ws, tool, log_prefix="skill_tools")


def create_python_tools(
    sp_ws: WorkspaceClient,
    config: dict[str, Any],
) -> list[FunctionTool]:
    """Build only ``type: python`` FunctionTools from ``TOOLS`` + ``SKILL_TOOLS``.

    Python function tools are first-party *in-process* tools, not the
    legacy direct-mode Genie / Vector Search tools the ``mcp_only`` flag
    gates.  This helper lets the runtime attach them **regardless** of
    ``mcp_only`` (see :func:`payloads.agent.runtime.tool_assembly.build_all_tools`): under
    ``mcp_only=True`` the two general factories are skipped, but python
    tools are still built here; under ``mcp_only=False`` they are built by
    ``create_tools`` / ``create_skill_tools`` instead, so this helper is
    not used on that path.

    Agent-level (``TOOLS``) entries win over skill-level (``SKILL_TOOLS``)
    ones of the same name, matching the agent-first dedup convention in
    :func:`create_skill_tools`.  Per-tool build failures fail soft.
    """
    python_cfgs = [
        tool_cfg
        for key in ("TOOLS", "SKILL_TOOLS")
        for tool_cfg in config.get(key, []) or []
        if isinstance(tool_cfg, dict) and tool_cfg.get("type") == "python"
    ]
    if not python_cfgs:
        return []

    ws = resolve_tools_client(sp_ws)
    tools: list[FunctionTool] = []
    seen: set[str] = set()

    for tool_cfg in python_cfgs:
        tool = _as_tool(tool_cfg)
        if tool is None or tool.name in seen:
            continue
        try:
            function_tool = create_function_tool(ws, tool, log_prefix="python_tools")
        except Exception as exc:
            logger.warning(
                "[python_tools] failed to create '%s' -- skipping: %s",
                tool.name,
                exc,
            )
            continue
        if function_tool is not None:
            tools.append(function_tool)
            seen.add(tool.name)

    logger.info("[python_tools] created %d python tool(s)", len(tools))
    return tools
