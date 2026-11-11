"""Tool list assembly shared by the chat entrypoint and the MCP/A2A runner.

Lives outside :mod:`payloads.agent.runtime.agent` because importing that
module wires up tracing and registers the MLflow ``@invoke``/``@stream``
handlers, which :mod:`payloads.agent.runtime.agent_runner` must not trigger.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agent_foundry._instance_layout import resolve_payload_subdir
from payloads.agent.runtime.instance_paths import resolve_instance_dir
from payloads.agent.runtime.lazy_skills import maybe_append_load_skill_tool
from payloads.agent.runtime.tool_builders import (
    create_python_tools,
    create_skill_tools,
    create_tools,
    warn_dropped_direct_tools,
)

if TYPE_CHECKING:
    from databricks.sdk import WorkspaceClient


def build_all_tools(
    ws: WorkspaceClient,
    config: dict[str, Any],
    variables: dict[str, str],
) -> list[Any]:
    """Merge agent-level tools, skill-declared tools, and (when in
    ``skill_loading: lazy`` mode) the built-in ``_load_skill`` tool.

    When ``config["MCP_ONLY"]`` is true (the framework default) the two
    direct factories (``create_tools`` / ``create_skill_tools``) are
    skipped entirely -- the agent attaches tools exclusively via the
    MCP servers from the ``bricks/mcp/`` brick.  Each suppressed
    direct-mode declaration is logged at WARNING by
    :func:`warn_dropped_direct_tools` so deployers see their config is
    being skipped rather than silently honoured.

    The lazy-skill tool is appended last so it never shadows a
    user-declared tool of the same name (the leading-underscore name
    convention also makes that effectively impossible).  See
    :func:`payloads.agent.runtime.lazy_skills.maybe_append_load_skill_tool` for
    the eager-mode no-op contract.
    """
    if config.get("MCP_ONLY", True):
        warn_dropped_direct_tools(config)
        # Python function tools are first-party in-process tools, not the
        # legacy direct-mode Genie/VS the ``MCP_ONLY`` flag gates, so they
        # are attached regardless of the flag.
        tools: list[Any] = create_python_tools(ws, config)
    else:
        agent_tools = create_tools(ws, config, variables)
        existing_names = {t.name for t in agent_tools}
        skill_tools = create_skill_tools(
            ws,
            config,
            variables,
            existing_tool_names=existing_names,
        )
        tools = agent_tools + skill_tools
    instance_dir = resolve_instance_dir()
    if instance_dir is not None:
        tools = maybe_append_load_skill_tool(tools, config, resolve_payload_subdir(instance_dir, "skills"))
    return tools
