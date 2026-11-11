"""
A2A Adapter
============
Bridges the Google Agent-to-Agent (A2A) protocol to the shared
``agent_runner`` using the ``a2a-sdk`` (v1.0).

Routes are injected directly into the existing ``AgentServer`` FastAPI
app via Starlette ``Route`` objects created by the SDK's route factories.

The public agent card served at ``/api/.well-known/agent-card.json`` is
authored per instance via ``spec.integrations.a2a.card`` in
``payloads/agent/payload.yml`` (see
:class:`agent_foundry.contracts.a2a.A2AAgentCardConfig`).  When the
instance does not author an ``a2a:`` block, this adapter falls back to
:data:`_FALLBACK_CARD` so unmigrated instances keep working unchanged.

Design references (not dependencies):
  - kagent-openai ``OpenAIAgentExecutor``  (streaming bridge pattern)
  - a2a-samples helloworld                (canonical event lifecycle)
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes.agent_card_routes import create_agent_card_routes
from a2a.server.routes.jsonrpc_routes import create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from a2a.types.a2a_pb2 import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentProvider,
    AgentSkill,
    Artifact,
    Message,
    Part,
    Role,
    Task,
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
)
from fastapi import FastAPI, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from agent_foundry.contracts.a2a import (
    A2AAgentCardConfig,
    A2AAgentSkill,
)
from payloads.agent.runtime.agent_runner import run_agent_streamed, set_request_headers

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Fallback card (preserves pre-typed-config behaviour for unmigrated instances)
# ---------------------------------------------------------------------------
#
# Before the typed ``a2a`` card block existed,
# every instance shipped the same hardcoded card with a single generic
# ``ask_agent`` skill.  Keeping that exact shape as the fallback means
# instances that have not authored an ``a2a:`` block see no behavioural
# change after this refactor -- only a one-line WARNING in the startup
# log nudging them to author one.

_FALLBACK_CARD = A2AAgentCardConfig(
    name="Foundry Agent",
    description="Databricks AI Agent exposed via A2A protocol",
    version="1.0.0",
    skills=[
        A2AAgentSkill(
            id="ask_agent",
            name="Ask Agent",
            description="Ask the agent a question and get a response",
            tags=["general"],
            examples=["What is the revenue for Q4?"],
        ),
    ],
)


class FoundryAgentExecutor(AgentExecutor):
    """Bridge between A2A protocol and the shared agent runner."""

    async def execute(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        task = context.current_task or Task(
            id=context.task_id,
            context_id=context.context_id,
            status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
        )
        await event_queue.enqueue_event(task)

        await event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                task_id=context.task_id,
                context_id=context.context_id,
                status=TaskStatus(
                    state=TaskState.TASK_STATE_WORKING,
                    message=Message(
                        message_id=str(uuid.uuid4()),
                        role=Role.ROLE_AGENT,
                        parts=[Part(text="Processing request...")],
                    ),
                ),
            )
        )

        user_text = context.get_user_input()
        full_text = ""
        is_error = False

        async for chunk in run_agent_streamed(user_text, source="a2a"):
            is_error = chunk.startswith("Error:")
            full_text += chunk
            await event_queue.enqueue_event(
                TaskArtifactUpdateEvent(
                    task_id=context.task_id,
                    context_id=context.context_id,
                    artifact=Artifact(
                        artifact_id=str(uuid.uuid4()),
                        parts=[Part(text=chunk)],
                    ),
                )
            )

        await event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                task_id=context.task_id,
                context_id=context.context_id,
                status=TaskStatus(
                    state=(TaskState.TASK_STATE_FAILED if is_error else TaskState.TASK_STATE_COMPLETED),
                    message=(
                        Message(
                            message_id=str(uuid.uuid4()),
                            role=Role.ROLE_AGENT,
                            parts=[Part(text=full_text)],
                        )
                        if is_error
                        else None
                    ),
                ),
            )
        )

    async def cancel(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        raise NotImplementedError("Cancellation not supported")


# ---------------------------------------------------------------------------
# Builder: A2AAgentCardConfig (contract) -> AgentCard (a2a-sdk proto)
# ---------------------------------------------------------------------------


def _build_skill(skill: A2AAgentSkill, card_input_modes: list[str], card_output_modes: list[str]) -> AgentSkill:
    """Map one contract skill to its proto counterpart.

    Per-skill ``input_modes`` / ``output_modes`` fall back to the
    card-level defaults when unset on the contract; this keeps the
    authored YAML minimal when every skill uses the same modes.
    """
    return AgentSkill(
        id=skill.id,
        name=skill.name,
        description=skill.description,
        tags=list(skill.tags),
        examples=list(skill.examples),
        input_modes=list(skill.input_modes if skill.input_modes is not None else card_input_modes),
        output_modes=list(skill.output_modes if skill.output_modes is not None else card_output_modes),
    )


def _build_provider(card: A2AAgentCardConfig) -> AgentProvider | None:
    """Emit a proto provider only when the contract carries at least one field.

    ``a2a-sdk`` accepts an empty ``AgentProvider`` but propagating an
    empty one would advertise blank organisation / url fields to
    clients; the contract treats both as optional, so we skip the
    field entirely when neither is set.
    """
    if card.provider is None:
        return None
    if not card.provider.organization and not card.provider.url:
        return None
    return AgentProvider(
        organization=card.provider.organization or "",
        url=card.provider.url or "",
    )


def build_agent_card(
    card: A2AAgentCardConfig,
    base_url: str = "",
    enable_v0_3_compat: bool = False,
) -> AgentCard:
    """Build an A2A AgentCard proto from a typed contract.

    Pure function: same input always produces the same output, no I/O,
    safe to call from tests.

    ``base_url`` is appended to ``/api/a2a`` for the supported
    interface URL when set (production deploys leave it empty so the
    interface URL is relative -- A2A clients resolve it against the
    app host at call time).

    ``enable_v0_3_compat`` controls whether a second ``AgentInterface``
    entry advertising ``protocol_version="0.3"`` is appended. v0.3
    clients fail to parse a v1.0-only card at discovery time, so this
    flag must be on whenever the SDK route factory's
    ``enable_v0_3_compat=True`` is used (see
    :func:`register_a2a_routes`); the two are coordinated together at
    the call site.
    """
    capabilities = AgentCapabilities(
        streaming=card.capabilities.streaming,
        push_notifications=card.capabilities.push_notifications,
    )

    provider = _build_provider(card)
    skills = [_build_skill(s, card.default_input_modes, card.default_output_modes) for s in card.skills]

    rpc_url = f"{base_url}/api/a2a" if base_url else "/api/a2a"
    interfaces = [
        AgentInterface(
            protocol_binding="JSONRPC",
            protocol_version="1.0",
            url=rpc_url,
        ),
    ]
    if enable_v0_3_compat:
        interfaces.append(
            AgentInterface(
                protocol_binding="JSONRPC",
                protocol_version="0.3",
                url=rpc_url,
            )
        )

    kwargs: dict[str, Any] = {
        "name": card.name,
        "description": card.description,
        "version": card.version,
        "default_input_modes": list(card.default_input_modes),
        "default_output_modes": list(card.default_output_modes),
        "capabilities": capabilities,
        "supported_interfaces": interfaces,
        "skills": skills,
    }
    if provider is not None:
        kwargs["provider"] = provider
    if card.documentation_url:
        kwargs["documentation_url"] = card.documentation_url
    if card.icon_url:
        kwargs["icon_url"] = card.icon_url

    return AgentCard(**kwargs)


# ---------------------------------------------------------------------------
# Config loader (instance ``a2a:`` block, with fallback)
# ---------------------------------------------------------------------------


def _resolve_card_and_flags() -> tuple[A2AAgentCardConfig, bool]:
    """Load the instance's A2A card config and runtime flags from ``payloads/agent/payload.yml``.

    Returns a tuple ``(card, enable_v0_3_compat)``. Falls back to
    ``(_FALLBACK_CARD, False)`` when:
      - the runtime config cannot be loaded (missing workspace client,
        etc. -- shouldn't happen at startup, but the A2A adapter must
        not bring the whole app down if it does), OR
      - the instance has not authored an ``a2a:`` block, OR
      - the authored ``a2a:`` block is present but has no ``card:``.

    Logs a WARNING in the fallback paths so deploys are visibly using
    the generic card.
    """
    try:
        from payloads.agent.runtime.request_context import get_workspace_client
        from payloads.agent.runtime.runtime_config import load_agent_config_and_vars

        ws = get_workspace_client()
        config, _variables = load_agent_config_and_vars(ws)
    except Exception as exc:
        logger.warning(
            "[a2a] failed to load agent config (%s); using fallback agent card",
            exc,
        )
        return _FALLBACK_CARD, False

    a2a_cfg = config.get("A2A")
    if a2a_cfg is None or a2a_cfg.card is None:
        logger.warning(
            "[a2a] no 'integrations.a2a.card' block in payloads/agent/payload.yml; using fallback "
            "agent card (name=%r). Author one to customize what external "
            "A2A consumers see at /api/.well-known/agent-card.json.",
            _FALLBACK_CARD.name,
        )
        return _FALLBACK_CARD, False
    return a2a_cfg.card, a2a_cfg.enable_v0_3_compat


class _OboHeaderMiddleware(BaseHTTPMiddleware):
    """Capture OBO headers on A2A requests so agent_runner can read them."""

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        if request.url.path.startswith("/api/a2a"):
            set_request_headers(dict(request.headers))
        return await call_next(request)


def register_a2a_routes(app: FastAPI, base_url: str = "") -> None:
    """Inject A2A protocol routes into an existing FastAPI app.

    The card's ``supported_interfaces`` and the JSON-RPC route's
    ``enable_v0_3_compat`` translation flag are kept in lockstep --
    both are derived from ``A2AConfig.enable_v0_3_compat`` at the
    instance config level, never split across call sites. v0.3
    clients fail at card discovery if only one of the two is enabled.
    """
    card, enable_v0_3_compat = _resolve_card_and_flags()
    agent_card = build_agent_card(card, base_url, enable_v0_3_compat=enable_v0_3_compat)

    handler = DefaultRequestHandler(
        agent_executor=FoundryAgentExecutor(),
        task_store=InMemoryTaskStore(),
        agent_card=agent_card,
    )

    card_routes = create_agent_card_routes(
        agent_card,
        card_url="/api/.well-known/agent-card.json",
    )
    rpc_routes = create_jsonrpc_routes(
        handler,
        rpc_url="/api/a2a",
        enable_v0_3_compat=enable_v0_3_compat,
    )

    for route in card_routes + rpc_routes:
        app.routes.append(route)

    app.add_middleware(_OboHeaderMiddleware)

    logger.info(
        "[a2a] routes registered at /api/a2a (card: name=%r, version=%r, skills=%d, v0_3_compat=%s)",
        card.name,
        card.version,
        len(card.skills),
        enable_v0_3_compat,
    )
