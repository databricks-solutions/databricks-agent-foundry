"""
Shared Agent Runner
====================
Provides ``run_agent()`` for MCP and A2A adapters to invoke the
agent without duplicating the full build-tools → connect-MCP →
create-agent → run lifecycle that lives in ``payloads.agent.runtime.agent``.

OBO / user-identity forwarding:
    ``mlflow.genai.agent_server.get_request_headers()`` only works
    inside ``@invoke()``/``@stream()`` handlers.  For MCP/A2A paths
    we store forwarded headers in a :class:`contextvars.ContextVar`
    that ``get_obo_token()`` and ``get_request_username()`` fall back
    to.  Callers must set the context via :func:`set_request_headers`
    before calling ``run_agent()``.
"""

from __future__ import annotations

import contextvars
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator

import mlflow
from agents import Runner
from openai.types.responses import ResponseOutputMessage

from payloads.agent.runtime.context_layers import prepare_context_layers
from payloads.agent.runtime.context_limit import fail_soft_notice
from payloads.agent.runtime.core import create_agent
from payloads.agent.runtime.data_universe import ensure_data_universe
from payloads.agent.runtime.gateway_selection import Selections
from payloads.agent.runtime.gateway_skills import prepare_gateway_skills
from payloads.agent.runtime.mcp_availability import user_notice
from payloads.agent.runtime.mcp_connect import managed_mcp_servers
from payloads.agent.runtime.reasoning import replay_scope
from payloads.agent.runtime.request_context import (
    get_request_username,
    get_user_workspace_client,
    get_workspace_client,
    new_request_state,
)
from payloads.agent.runtime.runtime_config import init_agent_module, load_agent_config_and_vars
from payloads.agent.runtime.tool_assembly import build_all_tools
from payloads.agent.runtime.tool_result_clearing import run_config_for, tool_schema_chars

logger = logging.getLogger(__name__)

_request_headers_var: contextvars.ContextVar[dict[str, str]] = contextvars.ContextVar(
    "agent_runner_request_headers",
    default={},
)


def set_request_headers(headers: dict[str, str]) -> contextvars.Token[dict[str, str]]:
    """Store forwarded request headers for the current async context.

    Call this from MCP/A2A adapter middleware before invoking
    ``run_agent()`` so that OBO identity resolution works.
    """
    return _request_headers_var.set(headers)


def get_fallback_headers() -> dict[str, str]:
    """Return headers stored by :func:`set_request_headers`, if any."""
    return _request_headers_var.get()


# ---------------------------------------------------------------------------
# Shared setup
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _prepare_agent_context() -> AsyncGenerator[tuple[Any, dict[str, Any], str], None]:
    """Build tools, connect MCP servers, create agent. Yields (agent, config, notice).

    *notice* is the user-facing unavailable-tools notice, empty when every
    configured MCP server is available.

    Each setup phase is wrapped in an MLflow span (``agent.config_load``,
    ``agent.tools_build``, ``agent.mcp_connect``) so cold-start latency
    can be bisected from the trace.  The MCP/A2A path does not run the
    PR 4 prompts/resources consumption layer, so no
    ``agent.consumption_layer`` span is emitted here -- contrast with
    :func:`payloads.agent.runtime.agent.stream_handler`.
    """
    init_agent_module()
    ws = get_workspace_client()
    with mlflow.start_span(name="agent.config_load", span_type="TASK"):
        config, variables = load_agent_config_and_vars(ws)

    # Via ``get_request_username`` rather than the header directly: it is
    # the one place that knows headers arrive latin-1 encoded, so reading
    # the key here would put a mangled name into the prompt.
    username = get_request_username()
    if username:
        config["_resolved_username"] = username

    with mlflow.start_span(
        name="agent.tools_build",
        span_type="TASK",
    ) as span:
        tools = build_all_tools(ws, config, variables)
        span.set_attribute("tools_count", len(tools))
        span.set_attribute("mcp_only", config.get("MCP_ONLY", True))

    async with managed_mcp_servers(config, ws) as mcp:
        agent = create_agent(
            config,
            tools=tools,
            mcp_servers=mcp.servers,
            data_universe=await ensure_data_universe(ws, config, variables),
            gateway_skills=await prepare_gateway_skills(config, ws, get_user_workspace_client(), Selections()),
            memory=(await prepare_context_layers(config, session_id=None)).memory,
            unavailable_mcp=mcp.unavailable,
        )
        yield agent, config, user_notice(mcp.unavailable)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@mlflow.trace(name="agent_runner.run", span_type="AGENT")
async def run_agent(question: str, *, source: str = "unknown") -> str:
    """Full lifecycle: build agent, run, return final text output.

    *source* is tagged on the MLflow trace (``mcp`` or ``a2a``).
    On failure returns a human-readable error string rather than raising,
    so adapters can forward the error cleanly to their protocol.
    """
    try:
        mlflow.update_current_trace(
            tags={"source": source},
            request_preview=question,
        )
    except Exception:
        pass

    try:
        async with _prepare_agent_context() as (agent, config, notice):
            state = new_request_state()
            schema_chars = await tool_schema_chars(agent, state)
            with replay_scope():
                result = await Runner.run(
                    starting_agent=agent,
                    input=question,
                    max_turns=config["MAX_ITERATIONS"],
                    run_config=run_config_for(config, tool_schema_chars=schema_chars),
                    context=state,
                )
            answer = result.final_output or ""
            return f"{notice}\n\n{answer}" if notice else answer
    except Exception as exc:
        logger.exception("[agent_runner] run_agent failed")
        return fail_soft_notice(exc) or f"Error: {exc}"


async def run_agent_streamed(question: str, *, source: str = "unknown") -> AsyncIterator[str]:
    """Streaming variant of :func:`run_agent`.

    Yields text chunks at **message level**: each complete assistant message
    is yielded as a single string.  Tool calls and intermediate events are
    not surfaced — only final text content the user should see.
    """
    with mlflow.start_span(name="agent_runner.run_streamed", span_type="AGENT") as span:
        span.set_inputs({"question": question, "source": source})
        try:
            mlflow.update_current_trace(
                tags={"source": source},
                request_preview=question,
            )
        except Exception:
            pass

        accumulated = ""
        try:
            async with _prepare_agent_context() as (agent, config, notice):
                if notice:
                    yield notice
                state = new_request_state()
                schema_chars = await tool_schema_chars(agent, state)
                # Only the call needs the scope, as in ``agent._run_stream``
                # (see there): the run is a task that keeps a copy of this
                # context.
                with replay_scope():
                    result = Runner.run_streamed(
                        starting_agent=agent,
                        input=question,
                        max_turns=config["MAX_ITERATIONS"],
                        run_config=run_config_for(config, tool_schema_chars=schema_chars),
                        context=state,
                    )
                async for event in result.stream_events():
                    if not (
                        event.type == "run_item_stream_event"
                        and event.name == "message_output_created"
                        and hasattr(event.item, "raw_item")
                    ):
                        continue
                    # ``raw_item`` is the 40+-item ``ResponseInputItem``
                    # union; only ``ResponseOutputMessage`` carries the
                    # ``content`` attribute we read below.  The
                    # ``message_output_created`` event-name filter above
                    # already guarantees this at runtime per the
                    # ``agents`` SDK contract -- the explicit
                    # ``isinstance`` makes that contract enforceable
                    # statically and self-defends against SDK drift.
                    raw_item = event.item.raw_item
                    if not isinstance(raw_item, ResponseOutputMessage):
                        continue
                    for content_part in raw_item.content:
                        if hasattr(content_part, "text") and content_part.text:
                            accumulated += content_part.text
                            yield content_part.text
            span.set_outputs({"response": accumulated})
        except Exception as exc:
            logger.exception("[agent_runner] run_agent_streamed failed")
            span.set_status("ERROR")
            span.set_outputs({"error": str(exc)})
            yield fail_soft_notice(exc) or f"Error: {exc}"
