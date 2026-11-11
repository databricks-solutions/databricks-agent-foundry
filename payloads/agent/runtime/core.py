"""
Shared Agent Core
==================
Helper functions shared by agent entrypoints (``agent.py``).
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, cast

from agents import Agent, FunctionTool, ModelSettings
from agents import Tool as SdkTool
from agents.mcp import MCPServer, MCPServerStreamableHttp

if TYPE_CHECKING:
    # Pulled in only for type annotations on ``create_agent``; the
    # runtime imports live in the callers to keep the import-cycle
    # profile unchanged.

    from payloads.agent.runtime.gateway_skills import GatewaySkills
    from payloads.agent.runtime.history_search import AgentHistorySearch
    from payloads.agent.runtime.memory import AgentMemory
    from payloads.agent.runtime.plan_tools import AgentPlan
    from payloads.agent.runtime.sql_result_read import AgentResultReader

from payloads.agent.runtime.instructions import (
    PlanRecitingInstructions,
    assemble_instructions,
    join_parts,
    resolve_identity_preamble,
)
from payloads.agent.runtime.mcp_availability import UnavailableServer, model_note
from payloads.agent.runtime.request_context import (
    get_workspace_client as get_workspace_client,
)
from payloads.agent.runtime.runtime_config import load_agent_config_and_vars as load_agent_config_and_vars
from payloads.agent.runtime.tool_builders import create_tools as create_tools

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Agent factory
# ---------------------------------------------------------------------------


def model_settings_for(config: dict[str, Any]) -> ModelSettings:
    """Build the agent's ``ModelSettings`` from the runtime config.

    Databricks is not the official OpenAI client, so openai-agents omits
    ``stream_options.include_usage`` unless it is set.  Without a usage
    chunk, MLflow autolog records 0 cache reads on the streamed chat path
    (#444).

    Thinking settings travel in ``extra_body`` rather than through
    ``ModelSettings.reasoning``: the endpoint accepts effort values (``max``)
    that openai's ``Reasoning`` type rejects, and ``thinking`` has no SDK
    field at all.  ``extra_body`` is left unset when nothing is authored,
    so the request body is identical to one built without these settings.

    ``display`` defaults to ``summarized`` for ``adaptive`` / ``enabled``
    thinking while the deployment emits reasoning: newer models return the
    summary empty unless asked, which would leave nothing to show.  The
    per-request ``include_reasoning`` deliberately plays no part, so every
    caller sends the same request and shares one prompt cache.
    """
    extra_body: dict[str, Any] = dict(config.get("LLM_EXTRA_BODY") or {})
    thinking = config.get("REASONING_THINKING")
    if thinking:
        block: dict[str, Any] = {"type": thinking}
        budget = config.get("REASONING_BUDGET_TOKENS")
        if budget is not None:
            block["budget_tokens"] = budget
        display = config.get("REASONING_DISPLAY")
        if display is None and thinking in {"adaptive", "enabled"} and config.get("REASONING_EMIT") is not False:
            display = "summarized"
        if display is not None:
            block["display"] = display
        extra_body["thinking"] = block
    effort = config.get("REASONING_EFFORT")
    if effort:
        extra_body["reasoning_effort"] = effort
    return ModelSettings(include_usage=True, extra_body=extra_body or None)


def create_agent(
    config: dict[str, Any],
    tools: list[FunctionTool] | None = None,
    mcp_servers: list[MCPServerStreamableHttp] | None = None,
    *,
    prompt_manuals: str = "",
    data_universe: str = "",
    synthetic_tools: list[FunctionTool] | None = None,
    gateway_skills: GatewaySkills | None = None,
    memory: AgentMemory | None = None,
    plan: AgentPlan | None = None,
    history_search: AgentHistorySearch | None = None,
    result_reader: AgentResultReader | None = None,
    unavailable_mcp: Sequence[UnavailableServer] = (),
) -> Agent:
    """Create an Agent instance from resolved config and wrapped tools.

    The instructions are a string ending in
    ``<user-tail sentinel><user tail><context sentinel><context>``.  On
    the way out, :mod:`payloads.agent.runtime.prompt_cache` turns it into
    a cached system message, an unmarked user-tail message after it, and
    the context as the last message, after the history.  Caching is
    cumulative from the start of the prompt, so the order follows how
    often each part changes: the body is shared by every request, the
    user tail (skill index, model note) varies per user, and the
    context (user, time, memory index, plan) changes between turns.

    Nothing is written into the conversation history either: a per-request
    stamp on a history message is only applied to the *newest* user turn,
    so on the next request that message arrives unstamped and the cache is
    invalidated from that point on, taking the previous turn's tool results
    with it.  The context message is dropped and rebuilt instead.

    *prompt_manuals* is the pre-rendered ``## Tool Manuals`` block from
    :func:`payloads.agent.runtime.mcp_consumption.collect_prompt_manuals`.  When
    non-empty it is appended to the system prompt so the LLM sees
    server-supplied tool documentation alongside the agent prompt.  It
    shares the cached prefix, so it must be stable across requests for
    caching to pay off -- hence the prompt-hash log line below.

    *data_universe* is the rendered catalog overview from
    :func:`payloads.agent.runtime.data_universe.ensure_data_universe`.  It is
    appended last, after the tool manuals, so the model reads the
    instructions before the reference data they describe.  It shares the
    cached prefix on the same terms as *prompt_manuals*.

    *synthetic_tools* are extra :class:`FunctionTool` instances created
    by :func:`~payloads.agent.runtime.mcp_consumption.synthesize_resource_tools`
    (one ``<prefix>read_resource`` per server with resources enabled).
    They are merged with *tools*; duplicate names are not de-duplicated
    here -- the deploy-time ``tools.name_prefix`` regex already
    guarantees no collision with framework-internal tools.

    *gateway_skills* (Unity Gateway skills mode) adds the shared skill
    block right after the system prompt, the per-user skill index to the
    uncached user tail, and its ``_load_skill`` tool.

    *memory* adds the memory tools and its usage guidance (cached, after the
    skill block) and puts the user's topic index in the context, after the
    identity preamble.  *plan* adds the plan tools and the planning
    guidance after it; the instructions are then a callable that the SDK
    resolves before every model call, so the chat's plan is recited last
    in its latest state.

    *history_search* (compaction on) adds ``_search_history`` and its
    guidance after the planning guidance.

    *result_reader* (a managed SQL server with the read-only tool mounted)
    adds ``_read_result`` and its guidance after that.

    *unavailable_mcp* names the configured MCP servers this request could
    not mount; the note goes into the uncached tail, so a server going
    down or coming back does not invalidate the prompt cache.

    The keyword-only arguments default to "no-op" so callers that have
    not migrated to the PR 4 surface keep their existing behaviour.
    """
    all_tools = list(tools or [])
    if synthetic_tools:
        all_tools.extend(synthetic_tools)
    base_prompt = config["SYSTEM_PROMPT"]
    if gateway_skills is not None:
        all_tools.extend(gateway_skills.tools)
        if gateway_skills.block:
            base_prompt = f"{base_prompt}\n\n{gateway_skills.block}"
    if memory is not None:
        all_tools.extend(memory.tools)
        base_prompt = f"{base_prompt}\n\n{memory.guidance}"
    if plan is not None:
        all_tools.extend(plan.tools)
        base_prompt = f"{base_prompt}\n\n{plan.guidance}"
    if history_search is not None:
        all_tools.extend(history_search.tools)
        base_prompt = f"{base_prompt}\n\n{history_search.guidance}"
    if result_reader is not None:
        all_tools.extend(result_reader.tools)
        base_prompt = f"{base_prompt}\n\n{result_reader.guidance}"
    if prompt_manuals:
        base_prompt = f"{base_prompt}\n\n{prompt_manuals}"
    if data_universe:
        base_prompt = f"{base_prompt}\n\n{data_universe}"
    # Tripwire for prefix instability: a cache read requires the marked
    # prefix to be byte-identical across requests, so a hash that moves
    # between requests explains a missing discount at a glance.  Hashes the
    # cacheable body only -- the tails are *expected* to vary, and
    # including them would make the tripwire fire on every new user.
    user_tail = join_parts(gateway_skills.index if gateway_skills else "", model_note(unavailable_mcp))
    context = join_parts(resolve_identity_preamble(config), memory.index if memory else "")
    instructions = assemble_instructions(base_prompt, user_tail, context)
    active_mcp = mcp_servers or []

    logger.info(
        "System prompt hash %s (%d chars cached, %d total)",
        hashlib.sha256(base_prompt.encode("utf-8")).hexdigest()[:12],
        len(base_prompt),
        len(instructions),
    )

    # ``Agent.__init__`` is typed as ``list[Tool]`` / ``list[MCPServer]``
    # (the broad SDK unions / base classes).  Our typed inputs are
    # narrower (``list[FunctionTool]`` / ``list[MCPServerStreamableHttp]``)
    # which is fine at runtime but blocked by ``list``'s invariance.
    # ``cast`` widens to the SDK's expected list types without copying.
    agent_tools = cast("list[SdkTool]", all_tools)
    agent_mcp = cast("list[MCPServer]", active_mcp)

    return Agent(
        name="Supervisor",
        instructions=(
            instructions if plan is None else PlanRecitingInstructions(base_prompt, user_tail, context, plan.state)
        ),
        model=config["LLM_ENDPOINT_NAME"],
        tools=agent_tools,
        mcp_servers=agent_mcp,
        model_settings=model_settings_for(config),
    )
