"""The plan and long-term memory of one request.

Both live in the chat-history Lakebase and are reached through the chat UI
server as the signed-in user, whose ``x-forwarded-user`` id the Apps proxy
set: memory is the user's (:mod:`payloads.agent.runtime.memory`), the plan
the chat's (:mod:`payloads.agent.runtime.plan_tools`).  The user and the
chat are bound here, never tool arguments.  See
``docs/explanation/agent-memory.md``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from payloads.agent.contracts import AgentMemoryRuntime, AgentPlanRuntime
from payloads.agent.runtime.memory import AgentMemory, prepare_memory
from payloads.agent.runtime.plan_tools import AgentPlan, prepare_plan
from payloads.agent.runtime.request_context import get_forwarded_headers

logger = logging.getLogger(__name__)

ACTOR_HEADER: Final = "x-forwarded-user"


@dataclass(frozen=True)
class ContextLayers:
    """The agent-kept layers of this request's context; ``None`` where off."""

    memory: AgentMemory | None = None
    plan: AgentPlan | None = None


async def _memory(settings: AgentMemoryRuntime | None, headers: Mapping[str, str]) -> AgentMemory | None:
    return await prepare_memory(settings, headers) if settings is not None else None


async def _plan(
    settings: AgentPlanRuntime | None, session_id: str | None, headers: Mapping[str, str], *, memory: bool
) -> AgentPlan | None:
    if settings is None or session_id is None:
        return None
    return await prepare_plan(settings, session_id, headers, memory=memory)


async def prepare_context_layers(config: dict[str, Any], *, session_id: str | None) -> ContextLayers:
    """This request's plan and memory; off without a trusted user.

    *session_id* is the conversation id, ``None`` on paths without one,
    which get no plan.
    """
    memory_settings = config.get("MEMORY")
    plan_settings = config.get("PLAN") if session_id is not None else None
    if not isinstance(memory_settings, AgentMemoryRuntime):
        memory_settings = None
    if not isinstance(plan_settings, AgentPlanRuntime):
        plan_settings = None
    if memory_settings is None and plan_settings is None:
        return ContextLayers()
    headers = get_forwarded_headers()
    if not headers.get(ACTOR_HEADER):
        logger.warning("Agent plan or memory is on but has no signed-in user")
        return ContextLayers()
    memory, plan = await asyncio.gather(
        _memory(memory_settings, headers),
        _plan(plan_settings, session_id, headers, memory=memory_settings is not None),
    )
    return ContextLayers(memory=memory, plan=plan)
