"""Shared typed-Tool to FunctionTool dispatch (issue #153).

Two call sites in :mod:`payloads.agent.runtime.core` build :class:`FunctionTool`
instances from declarative tool entries:

* :func:`payloads.agent.runtime.tool_builders.create_tools` -- agent-level
  ``config["TOOLS"]`` (the ``AgentConfig.tools_typed`` field, see #67-E).
* :func:`payloads.agent.runtime.tool_builders.create_skill_tools` -- skill-level
  ``config["SKILL_TOOLS"]`` produced by ``payloads.agent.deploy.skills.assemble``.

Before this module both call sites carried their own per-type
factory dispatch.  They had drifted: ``create_tools`` parsed raw
dicts while ``create_skill_tools`` used the typed
:class:`agent_foundry.contracts.Tool` discriminated union, and the
optional-vector-search-kwargs handling was implemented twice with
slightly different conventions.

This module is the single source of truth for "given a typed
:class:`Tool`, build the matching :class:`FunctionTool` (or skip)".
The two call sites still own variable fallbacks and dedup logic; the
heart of the dispatch lives here.

Lives under :mod:`payloads.agent.runtime` (runtime-only namespace)
because :class:`FunctionTool` comes from the ``[runtime]`` extra
(``openai-agents``), and we keep the deploy slice import-light.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from agents import FunctionTool

from agent_foundry.contracts import (
    GenieTool,
    MCPTool,
    PythonFunctionTool,
    Tool,
    UCFunctionTool,
    VectorSearchTool,
)
from payloads.agent.runtime.tools import (
    _infer_params_schema,
    create_genie_tool,
    create_python_function_tool,
    create_vector_search_tool,
    import_python_callable,
)

if TYPE_CHECKING:
    from databricks.sdk import WorkspaceClient

logger = logging.getLogger(__name__)


_VS_OPTIONAL_KWARGS: tuple[str, ...] = (
    "columns",
    "query_type",
    "num_results",
    "score_threshold",
)


def create_function_tool(
    ws: WorkspaceClient,
    tool: Tool,
    *,
    fallback_space_id: str = "",
    fallback_index_name: str = "",
    log_prefix: str = "skill_tools",
) -> FunctionTool | None:
    """Build a :class:`FunctionTool` for *tool*, or return ``None``.

    Returns ``None`` (with a warning) when:

    - The tool is MCP-served (handled by the ``bricks/mcp/`` brick).
    - A required runtime field is missing and no fallback is supplied.
    - The tool type is not known to this factory.

    *fallback_space_id* / *fallback_index_name* support the legacy
    agent-level ``TOOLS`` path that resolves the resource ID from
    ``variables`` rather than the tool entry itself; skill-level
    callers leave them empty.

    *log_prefix* is the bracketed tag used in log output so the same
    helper produces ``[skill_tools] ...`` from one call site and
    ``[tools] ...`` from the other.
    """
    match tool:
        case GenieTool():
            space_id = tool.space_id or fallback_space_id
            if not space_id:
                logger.warning(
                    "[%s] genie tool '%s' has no space_id",
                    log_prefix,
                    tool.name,
                )
                return None
            logger.info(
                "[%s] creating Genie tool '%s' for space %s",
                log_prefix,
                tool.name,
                space_id,
            )
            return create_genie_tool(ws, space_id, name=tool.name)

        case VectorSearchTool():
            index_name = tool.index_name or fallback_index_name
            if not index_name:
                logger.warning(
                    "[%s] vector_search tool '%s' has no index_name",
                    log_prefix,
                    tool.name,
                )
                return None
            kwargs = _vs_optional_kwargs(tool)
            logger.info(
                "[%s] creating Vector Search tool '%s' for index %s",
                log_prefix,
                tool.name,
                index_name,
            )
            return create_vector_search_tool(
                ws,
                index_name,
                name=tool.name,
                **kwargs,
            )

        case PythonFunctionTool():
            # First-party in-process callable.  Import + introspection
            # failures fail soft (log + skip) so a bad ``callable_path``
            # never crashes the agent build -- mirroring the other arms.
            try:
                fn = import_python_callable(tool.callable_path)
            except Exception as exc:
                logger.warning(
                    "[%s] python tool '%s' unavailable (callable_path=%r): %s",
                    log_prefix,
                    tool.name,
                    tool.callable_path,
                    exc,
                )
                return None
            schema = tool.params_json_schema or _infer_params_schema(fn)
            logger.info(
                "[%s] creating python tool '%s' -> %s",
                log_prefix,
                tool.name,
                tool.callable_path,
            )
            return create_python_function_tool(
                tool.name,
                tool.description,
                fn,
                schema,
            )

        case UCFunctionTool() | MCPTool():
            # MCP-served tools are attached at agent build time via the
            # ``bricks/mcp/`` brick rather than as plain FunctionTools.
            # The skills assembler already filters these out of
            # ``SKILL_TOOLS`` (see ``_RUNTIME_IGNORED_TOOL_TYPES``); the
            # match arm guards against hand-edited configs.
            logger.debug(
                "[%s] '%s' (%s) handled via MCP brick -- skipping FunctionTool",
                log_prefix,
                tool.name,
                tool.type,
            )
            return None

        case _:
            logger.warning(
                "[%s] unsupported tool type for '%s' -- skipping",
                log_prefix,
                getattr(tool, "name", "?"),
            )
            return None


def _vs_optional_kwargs(tool: VectorSearchTool) -> dict[str, Any]:
    """Pass only kwargs the user actually set in YAML, so defaults
    stay under :func:`payloads.agent.runtime.tools.create_vector_search_tool`'s
    signature rather than leaking ``VectorSearchTool`` model defaults.
    """
    set_fields = tool.model_fields_set
    kwargs: dict[str, Any] = {}
    for key in _VS_OPTIONAL_KWARGS:
        if key not in set_fields:
            continue
        value = getattr(tool, key)
        if key == "score_threshold" and value is None:
            continue
        kwargs[key] = value
    return kwargs
