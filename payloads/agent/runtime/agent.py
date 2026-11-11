"""
Supervisor Agent -- Databricks Apps Entrypoint
================================================
Registers ``@invoke()`` and ``@stream()`` handlers that the MLflow
AgentServer discovers when deployed as a Databricks App.

Core agent-building logic lives in :mod:`payloads.agent.runtime.core` and is
shared with the Model Serving adapter.

Auth model:
  - Tool calls (Genie, Vector Search) prefer the user's OBO token
    and fall back to the app's service principal.
  - UC Function tools use Databricks Managed MCP with the user's OBO
    token (``mcp.functions`` scope).

Local testing::

    INSTANCE_DIR=instances/<your-app> python -m payloads.agent.runtime.agent
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, AsyncGenerator

import mlflow
from agents import Runner

if TYPE_CHECKING:
    # Type-only imports for the quoted forward-references below.
    # ``WorkspaceClient`` and ``Agent`` are runtime-imported lazily inside
    # the helpers that need them (see ``payloads.agent.runtime.core``); we
    # only need the symbols visible to type checkers and ``get_type_hints``
    # to keep ruff (F821) and mypy honest.
    from agents import Agent
    from databricks.sdk import WorkspaceClient
from mlflow.genai.agent_server import invoke, stream
from mlflow.types.responses import (
    ResponsesAgentRequest,
    ResponsesAgentResponse,
    ResponsesAgentStreamEvent,
)

from agent_foundry import env
from payloads.agent.runtime.context_layers import prepare_context_layers
from payloads.agent.runtime.context_limit import fail_soft_notice, is_context_limit_error
from payloads.agent.runtime.context_occupancy import (
    OccupancyRecorder,
    context_window_outputs,
    merge_custom_outputs,
    reported_input_tokens,
)
from payloads.agent.runtime.core import create_agent
from payloads.agent.runtime.data_universe import ensure_data_universe
from payloads.agent.runtime.extensions import RequestContext, extension_tools, load_extensions
from payloads.agent.runtime.gateway_selection import Selections, parse_selections
from payloads.agent.runtime.gateway_skills import GatewaySkills, prepare_gateway_skills
from payloads.agent.runtime.history_search import prepare_history_search
from payloads.agent.runtime.lazy_skills import load_skill_invoke, without_load_skill
from payloads.agent.runtime.mcp_availability import user_notice
from payloads.agent.runtime.mcp_connect import managed_mcp_servers
from payloads.agent.runtime.mcp_consumption import (
    CachedManual,
    collect_prompt_manuals,
    synthesize_resource_tools,
)
from payloads.agent.runtime.mcp_wrapper import FilteredMCPServerStreamableHttp
from payloads.agent.runtime.reasoning import (
    parse_replay_entries,
    regroup_parallel_calls,
    replay_outputs,
    replay_scope,
    resolve_emit_reasoning,
)
from payloads.agent.runtime.request_context import (
    get_forwarded_headers,
    get_request_username,
    get_user_workspace_client,
    get_workspace_client,
    new_request_state,
)
from payloads.agent.runtime.responses_input import (
    restore_tool_output_lists,
    sanitize_responses_input,
    uniquify_function_call_ids,
)
from payloads.agent.runtime.runtime_config import init_agent_module, load_agent_config_and_vars
from payloads.agent.runtime.sql_result_read import prepare_result_reader
from payloads.agent.runtime.stream_utils import (
    build_message_event,
    build_message_item,
    process_agent_stream_events,
    start_early_tool_outputs,
)
from payloads.agent.runtime.tool_assembly import build_all_tools
from payloads.agent.runtime.tool_result_clearing import run_config_for, tool_schema_chars
from payloads.agent.runtime.tools import import_python_callable
from payloads.agent.runtime.tracing import setup_tracing_early
from payloads.agent.runtime.user_skills import prepare_user_skills

# Configure trace destination before the server starts accepting requests.
# The @stream/@invoke decorators create trace spans before the handler body
# runs, so the exporter must be wired up at import time.
setup_tracing_early()

logger = logging.getLogger(__name__)

init_agent_module()


def _filtered_brick_servers(
    servers: list[Any],
) -> list[FilteredMCPServerStreamableHttp]:
    """Narrow *servers* to brick-state instances only.

    Legacy ``SKILL_TOOLS`` servers are plain
    :class:`agents.mcp.MCPServerStreamableHttp` and have no
    ``prompts`` / ``resources`` config attached, so the consumption
    module would skip them anyway.  Filtering up front keeps the
    boundary explicit and lets the consumption module rely on the
    presence of brick-state attributes without defensive ``getattr``s.
    """
    return [s for s in servers if isinstance(s, FilteredMCPServerStreamableHttp)]


def _conversation_id(request: ResponsesAgentRequest) -> str | None:
    """Return ``request.context.conversation_id`` if present, else None.

    Centralised so the cache-key resolution in
    :mod:`payloads.agent.runtime.mcp_consumption` and any future per-conversation
    consumers see a single source of truth (and so the call-sites in
    invoke / stream stay symmetric).
    """
    ctx = request.context
    return ctx.conversation_id if ctx and ctx.conversation_id else None


def _request_context(request: ResponsesAgentRequest) -> RequestContext:
    """Identity extension tools may key state on.

    ``user_id`` is the value the caller sent in ``request.context``, never
    the display name :func:`_resolve_username` prefers for the prompt.
    """
    ctx = request.context
    return RequestContext(
        conversation_id=_conversation_id(request),
        user_id=ctx.user_id if ctx and ctx.user_id else None,
        headers=get_forwarded_headers(),
    )


def _extract_user_question(request: ResponsesAgentRequest) -> str | None:
    """Return the plain-text user question from the last user message."""
    for item in reversed(request.input or []):
        if getattr(item, "role", None) != "user":
            continue
        content = getattr(item, "content", None)
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            for part in content:
                text: str | None = getattr(part, "text", None) or (part.get("text") if isinstance(part, dict) else None)
                if text:
                    return text
    return None


def _trace_metadata(
    request: ResponsesAgentRequest,
    config: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Build MLflow trace metadata dict from the request context.

    User resolution order: OBO header → request.context.user_id → deployer
    (workspace current user resolved at startup).
    """
    metadata: dict[str, str] = {}

    username = get_request_username()
    if username:
        metadata["mlflow.trace.user"] = username

    ctx = request.context
    if ctx and ctx.conversation_id:
        metadata["mlflow.trace.session"] = ctx.conversation_id
    if ctx and ctx.user_id:
        metadata.setdefault("mlflow.trace.user", ctx.user_id)

    if "mlflow.trace.user" not in metadata and config:
        deployer = config.get("DEPLOYER_USER")
        if deployer:
            metadata["mlflow.trace.user"] = deployer

    return metadata


def _link_prompt(config: dict[str, Any]) -> None:
    """Load the registered prompt via URI to auto-link it to the active trace.

    Uses the ``prompts:/<name>/<version>`` URI format which is the
    canonical way to trigger MLflow's trace-to-prompt linkage.
    """
    prompt_uri = config.get("PROMPT_URI")
    if not prompt_uri:
        return
    try:
        mlflow.genai.load_prompt(prompt_uri)
    except Exception:
        pass


def _build_prompt_tag(config: dict[str, Any]) -> str | None:
    """Return a JSON string for the ``mlflow.linkedPrompts`` trace tag."""
    name = config.get("PROMPT_NAME")
    version = config.get("PROMPT_VERSION")
    if not name:
        return None
    import json

    return json.dumps([{"name": name, "version": str(version or "1")}])


def _tag_outer_trace(
    request: ResponsesAgentRequest,
    config: dict[str, Any],
) -> None:
    """Best-effort tagging of the outermost trace created by @invoke/@stream.

    The AgentServer decorator may create a parent trace before our
    @mlflow.trace-decorated function runs.  Setting metadata and
    prompt tag here ensures the outer trace is also populated.
    ``source: ui`` marks ``/invocations`` traces, alongside the
    ``mcp`` / ``a2a`` values set by ``agent_runner``.
    """
    try:
        metadata = _trace_metadata(request, config)
        tags: dict[str, str] = {"source": "ui"}
        prompt_tag = _build_prompt_tag(config)
        if prompt_tag:
            tags["mlflow.linkedPrompts"] = prompt_tag
        question = _extract_user_question(request)
        mlflow.update_current_trace(
            metadata=metadata or None,
            tags=tags,
            request_preview=question,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Generic lifecycle hooks (finalize / reset) -- Feature: custom_outputs
# ---------------------------------------------------------------------------
# Two optional dotted-path callables, authored via ``spec.runtime.reset``
# and ``spec.runtime.finalize`` (surfaced as ``RESET_CALLABLE`` /
# ``FINALIZE_CALLABLE`` in the runtime config).  They are fully generic:
# the framework never inspects what they do.  Both receive the request's
# ``RequestState`` (when they declare ``state``): ``reset`` runs at request
# start and seeds ``state.data`` before any ``type: python`` tool runs;
# ``finalize`` runs after ``Runner.run`` and its returned dict is merged
# into ``ResponsesAgentResponse.custom_outputs`` -- the out-of-band channel
# for handing back an assembled object without stuffing it into prose.
# Hook paths load through the same fail-closed instance-dir check as tool
# ``callable_path``s (see ``tools.import_python_callable``).


async def _call_hook(fn: Any, **available: Any) -> Any:
    """Call *fn*, passing only the kwargs it declares (or all when it takes
    ``**kwargs``), and awaiting the result if it is a coroutine.

    Lets a hook opt into ``request`` / ``result`` by simply declaring the
    parameter, and keeps a zero-arg hook (e.g. ``reset()``) trivial.
    """
    params: Mapping[str, inspect.Parameter]
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        params = {}
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
        kwargs = dict(available)
    else:
        kwargs = {k: v for k, v in available.items() if k in params}
    result = fn(**kwargs)
    if inspect.iscoroutine(result):
        result = await result
    return result


async def _run_reset(
    config: dict[str, Any] | None,
    request: ResponsesAgentRequest,
    state: Any = None,
) -> None:
    """Invoke the configured ``reset`` hook, if any (best-effort).

    Forwards ``request`` so the hook can read request-scoped inputs at
    request start (e.g. ``request.custom_inputs`` or
    ``request.context.conversation_id``), and ``state`` -- the same
    request-scoped :class:`~payloads.agent.runtime.request_context.RequestState` that is
    passed as ``Runner.run(context=...)`` -- so the hook can seed
    ``state.data`` before any tool runs.  A hook opts into either by simply
    declaring the parameter; :func:`_call_hook` passes only the kwargs the
    callable declares, so a zero-arg (or ``request``-only) reset hook is
    unaffected.
    """
    path = (config or {}).get("RESET_CALLABLE")
    if not path:
        return
    try:
        await _call_hook(import_python_callable(path), request=request, state=state)
    except Exception:
        logger.warning("[hooks] reset callable %r failed", path, exc_info=True)


async def _run_finalize(
    config: dict[str, Any] | None,
    request: ResponsesAgentRequest,
    result: Any,
    state: Any = None,
) -> dict[str, Any] | None:
    """Invoke the configured ``finalize`` hook and return its dict, if any.

    Forwards ``request`` / ``result`` and ``state`` -- the same
    request-scoped :class:`~payloads.agent.runtime.request_context.RequestState` the
    tools accumulated on during the run -- so the hook can read back
    ``state.data`` and map it into ``custom_outputs``.  A hook opts into any
    of them by declaring the parameter (:func:`_call_hook`).

    Returns ``None`` when no hook is configured, the hook raises, it returns
    a non-dict, or it returns an *empty* dict (all logged where relevant and
    swallowed -- a finalize failure must never fail the request).  Collapsing
    an empty dict to ``None`` keeps the invoke and stream paths symmetric:
    the stream path only emits a trailing event for a truthy payload, so the
    invoke path must not set ``custom_outputs={}`` where the stream path
    would emit nothing.
    """
    path = (config or {}).get("FINALIZE_CALLABLE")
    if not path:
        return None
    try:
        out = await _call_hook(import_python_callable(path), request=request, result=result, state=state)
    except Exception:
        logger.warning("[hooks] finalize callable %r failed", path, exc_info=True)
        return None
    if out is None:
        return None
    if not isinstance(out, dict):
        logger.warning(
            "[hooks] finalize callable %r returned %s (expected dict) -- ignoring",
            path,
            type(out).__name__,
        )
        return None
    return out or None


def _custom_outputs_event(custom_outputs: dict[str, Any]) -> ResponsesAgentStreamEvent:
    """Build a trailing ``response.completed`` stream event that carries
    *custom_outputs*.

    The streaming counterpart of setting ``ResponsesAgentResponse.custom_outputs``
    on the invoke path.  A ``response.completed`` event needs a full
    ``response`` envelope, so we wrap an empty-output
    :class:`ResponsesAgentResponse` -- the output items were already
    streamed as their own events; this final event exists only to deliver
    the out-of-band payload.

    ``custom_outputs`` is set both at the event's top level (the documented
    stream-event channel) and inside the response envelope, so a consumer
    reading either shape sees it.
    """
    return ResponsesAgentStreamEvent(
        type="response.completed",
        custom_outputs=custom_outputs,
        response=ResponsesAgentResponse(output=[], custom_outputs=custom_outputs).model_dump(),
    )


def _chat_notice(exc: BaseException) -> str | None:
    """The fail-soft notice for a chat turn (``/invocations``).

    With compaction on, the chat UI summarizes the conversation when the
    turn reports ``limit_exceeded``, so the notice says to retry instead.
    """
    return fail_soft_notice(exc, compaction=env.read_bool(env.ENABLE_COMPACTION))


@mlflow.trace(name="invoke_agent", span_type="AGENT")
async def _run_invoke(
    agent: "Agent",
    messages: list[Any],
    max_turns: int,
    request: ResponsesAgentRequest,
    config: dict[str, Any] | None = None,
) -> ResponsesAgentResponse:
    """Traced wrapper around Runner.run — creates the MLflow trace context."""
    metadata = _trace_metadata(request, config)
    question = _extract_user_question(request)
    mlflow.update_current_trace(
        metadata=metadata or None,
        request_preview=question,
    )
    if config:
        _link_prompt(config)

    # One RequestState per request, shared by reset (seed) -> tools
    # (accumulate) -> finalize (read), all via the same instance.
    state = new_request_state(request)
    await _run_reset(config, request, state)
    recorder = OccupancyRecorder()
    schema_chars = await tool_schema_chars(agent, state) if config else 0
    replay_seed = parse_replay_entries(request.custom_inputs)

    try:
        with replay_scope(replay_seed or ()) as replay_store:
            result = await Runner.run(
                starting_agent=agent,
                input=regroup_parallel_calls(messages, replay_seed or ()),
                max_turns=max_turns,
                run_config=run_config_for(config, recorder, schema_chars) if config else None,
                context=state,
            )
    except Exception as exc:
        notice = _chat_notice(exc)
        if notice is None:
            raise
        # Returning rather than re-raising is the point: the chat UI falls
        # back to a non-streaming retry on failure, and that retry would
        # re-send the same rejected history.
        logger.error("Run ended early, returning fail-soft notice: %s", exc)
        return ResponsesAgentResponse(
            output=[build_message_item(notice)],
            custom_outputs=context_window_outputs(recorder, limit_exceeded=is_context_limit_error(exc)),
        )
    emit_reasoning = resolve_emit_reasoning(config, request.custom_inputs)
    return ResponsesAgentResponse(
        output=[item.to_input_item() for item in result.new_items if emit_reasoning or item.type != "reasoning_item"],
        custom_outputs=merge_custom_outputs(
            await _run_finalize(config, request, result, state),
            context_window_outputs(recorder, reported_input_tokens=reported_input_tokens(result.raw_responses)),
            replay_outputs(replay_store, export=replay_seed is not None and emit_reasoning),
        ),
    )


def _with_notice(response: ResponsesAgentResponse, notice: str) -> ResponsesAgentResponse:
    """*response* with *notice* as a leading assistant message; unchanged when empty."""
    if not notice:
        return response
    return ResponsesAgentResponse.model_validate(
        {**response.model_dump(exclude_none=True), "output": [build_message_item(notice), *response.output]}
    )


@mlflow.trace(name="AgentRunner.run", span_type="AGENT")
async def _run_stream(
    agent: "Agent",
    messages: list[Any],
    max_turns: int,
    request: ResponsesAgentRequest,
    config: dict[str, Any] | None = None,
) -> AsyncGenerator[ResponsesAgentStreamEvent, None]:
    """Traced wrapper around Runner.run_streamed.

    The span name ``AgentRunner.run`` matches what ``_patched_agent_run``
    creates for the non-streamed path.  ``MlflowOpenAgentTracingProcessor``
    detects this name and **reuses** the span rather than creating a
    separate orphaned trace, keeping all child spans (tool, generation)
    nested inside the ``@stream()`` trace.
    """
    metadata = _trace_metadata(request, config)
    question = _extract_user_question(request)
    mlflow.update_current_trace(
        metadata=metadata or None,
        request_preview=question,
    )
    if config:
        _link_prompt(config)

    # One RequestState per request, shared by reset (seed) -> tools
    # (accumulate) -> finalize (read), all via the same instance.
    state = new_request_state(request)
    await _run_reset(config, request, state)
    recorder = OccupancyRecorder()
    schema_chars = await tool_schema_chars(agent, state) if config else 0
    replay_seed = parse_replay_entries(request.custom_inputs)
    emit_reasoning = resolve_emit_reasoning(config, request.custom_inputs)

    # Only the call needs the scope: ``run_streamed`` starts the run as a
    # task before it returns, and the task keeps a copy of this context,
    # store included, for as long as it runs.  Holding the scope across the
    # ``yield``s below would instead reset the ContextVar from whichever
    # context drains or closes this generator.  If an SDK upgrade defers
    # the task, replay stops and
    # ``test_streamed_tool_loop_reaches_invocations_events`` fails.
    hooks, early_tool_outputs = start_early_tool_outputs()
    with replay_scope(replay_seed or ()) as replay_store:
        result = Runner.run_streamed(
            starting_agent=agent,
            input=regroup_parallel_calls(messages, replay_seed or ()),
            max_turns=max_turns,
            hooks=hooks,
            run_config=run_config_for(config, recorder, schema_chars) if config else None,
            context=state,
        )
    try:
        events = process_agent_stream_events(
            result.stream_events(),
            emit_reasoning=emit_reasoning,
            early_tool_outputs=early_tool_outputs,
        )
        async for event in events:
            yield event
    except Exception as exc:
        notice = _chat_notice(exc)
        if notice is None:
            raise
        # The rejection surfaces while draining the stream, not from
        # ``run_streamed`` itself.  Yielding the notice instead of raising
        # keeps the stream successful, so the UI does not retry.
        logger.error("Run ended early mid-stream, yielding notice: %s", exc)
        yield build_message_event(notice)
        notice_outputs = context_window_outputs(recorder, limit_exceeded=is_context_limit_error(exc))
        if notice_outputs:
            yield _custom_outputs_event(notice_outputs)
        return
    # Deliver the finalize hook's structured output and the context entry
    # out-of-band as a trailing ``response.completed`` event (the
    # streaming counterpart of ``ResponsesAgentResponse.custom_outputs``).
    custom_outputs = merge_custom_outputs(
        await _run_finalize(config, request, result, state),
        context_window_outputs(recorder, reported_input_tokens=reported_input_tokens(result.raw_responses)),
        replay_outputs(replay_store, export=replay_seed is not None and emit_reasoning),
    )
    if custom_outputs:
        yield _custom_outputs_event(custom_outputs)


def _resolve_username(request: ResponsesAgentRequest) -> str:
    """OBO header → request.context.user_id → 'unknown'."""
    user = get_request_username()
    if not user:
        ctx = request.context
        if ctx and ctx.user_id:
            user = ctx.user_id
    return user or "unknown"


async def _build_consumption_layer(
    request: ResponsesAgentRequest,
    mcp_servers: list[Any],
    request_cache: dict[tuple[str, str], CachedManual],
) -> tuple[str, list[Any]]:
    """Run the PR 4 prompts/resources pass for one request.

    Returns ``(prompt_manuals, synthetic_tools)`` so the caller can pass
    them through to :func:`payloads.agent.runtime.core.create_agent`.  Both
    outputs are safe defaults when no brick-state servers are present
    or none has prompts/resources enabled -- in that case the agent
    behaves exactly as it did before PR 4.

    Wrapped in an ``agent.consumption_layer`` MLflow span so the trace
    can isolate prompts/resources fetch cost from MCP connect and
    tool build cost.  When tracing is disabled the span context
    manager is a no-op (``NoOpTracerProvider``) so the wrapper has
    near-zero overhead in production.
    """
    with mlflow.start_span(
        name="agent.consumption_layer",
        span_type="TASK",
    ) as span:
        brick_servers = _filtered_brick_servers(mcp_servers)
        span.set_attribute("brick_servers", len(brick_servers))
        if not brick_servers:
            return "", []
        prompt_manuals = await collect_prompt_manuals(
            brick_servers,
            conversation_id=_conversation_id(request),
            request_cache=request_cache,
        )
        synthetic_tools = synthesize_resource_tools(brick_servers)
        span.set_attribute("synthetic_tools", len(synthetic_tools))
        span.set_attribute("manuals_chars", len(prompt_manuals))
        return prompt_manuals, synthetic_tools


def _prepare_request_setup(
    request: ResponsesAgentRequest,
) -> tuple[Any, dict[str, Any], list[Any], list[dict[str, Any]], dict[str, str]]:
    """Run the per-request setup phase shared by both handlers.

    Returns ``(ws, config, tools, messages, variables)``.  Each major step is
    wrapped in an MLflow span so the trace can attribute cold-start
    latency to ``config_load`` (workspace state read), ``tools_build``
    (tool factories + skill tool dispatch), and the input sanitisation
    pass.  ``mcp_connect`` and ``consumption_layer`` are spanned by
    their respective code paths -- splitting them per concern keeps
    the trace flat and readable instead of nesting everything under
    one giant ``setup`` span.
    """
    ws = get_workspace_client()
    with mlflow.start_span(
        name="agent.config_load",
        span_type="TASK",
    ):
        config, variables = load_agent_config_and_vars(ws)
    _tag_outer_trace(request, config)
    config["_resolved_username"] = _resolve_username(request)
    with mlflow.start_span(
        name="agent.tools_build",
        span_type="TASK",
    ) as span:
        tools = build_all_tools(ws, config, variables)
        tools += extension_tools(load_extensions(), _request_context(request))
        span.set_attribute("tools_count", len(tools))
        span.set_attribute("mcp_only", config.get("MCP_ONLY", True))
    raw = [item.model_dump() for item in request.input]
    # Uniquify duplicate ``function_call`` ``id`` values before
    # ``sanitize_responses_input`` so the downstream
    # ``Runner.run`` -> ``deduplicate_input_items_preferring_latest``
    # pass in the OpenAI Agents SDK does not collapse multiple tool
    # calls emitted in the same assistant turn (issue: orphan
    # ``tool_result`` blocks reaching the model).  See module docstring
    # in ``responses_input.uniquify_function_call_ids`` for the full root cause.
    messages = restore_tool_output_lists(sanitize_responses_input(uniquify_function_call_ids(raw)))
    return ws, config, tools, messages, variables


async def _request_skills(
    config: dict[str, Any],
    ws: WorkspaceClient,
    request: ResponsesAgentRequest,
    tools: list[Any],
    selections: Selections,
) -> tuple[list[Any], GatewaySkills]:
    """This request's tools and skills: the gateway skills, then the user's Lakebase skills.

    One ``_load_skill`` serves every source: each loader hands the ids it
    does not own to the one before it, ending at the repo's lazy skills.
    """
    repo_loader = load_skill_invoke(tools)
    skills = await prepare_gateway_skills(config, ws, get_user_workspace_client(), selections, fallback=repo_loader)
    user_skills = prepare_user_skills(
        config, request.custom_inputs, skills, load_skill_invoke(skills.tools) or repo_loader
    )
    if user_skills is not None:
        skills = user_skills
    return (without_load_skill(tools) if skills.tools else tools), skills


@invoke()
async def invoke_handler(request: ResponsesAgentRequest) -> ResponsesAgentResponse:
    """Non-streaming invocation handler registered with MLflow AgentServer."""
    ws, config, tools, messages, variables = _prepare_request_setup(request)
    selections = parse_selections(request.custom_inputs)

    async with managed_mcp_servers(config, ws, selections) as mcp:
        prompt_manuals, synthetic_tools = await _build_consumption_layer(
            request,
            mcp.servers,
            mcp.request_cache,
        )
        agent_tools, skills = await _request_skills(config, ws, request, tools, selections)
        layers = await prepare_context_layers(config, session_id=_conversation_id(request))
        agent = create_agent(
            config,
            tools=agent_tools,
            mcp_servers=mcp.servers,
            prompt_manuals=prompt_manuals,
            data_universe=await ensure_data_universe(ws, config, variables),
            synthetic_tools=synthetic_tools,
            gateway_skills=skills,
            memory=layers.memory,
            plan=layers.plan,
            history_search=prepare_history_search(_conversation_id(request), get_forwarded_headers()),
            result_reader=prepare_result_reader(mcp.servers),
            unavailable_mcp=mcp.unavailable,
        )
        response = await _run_invoke(agent, messages, config["MAX_ITERATIONS"], request, config)
        return _with_notice(response, user_notice(mcp.unavailable))


@stream()
async def stream_handler(
    request: ResponsesAgentRequest,
) -> AsyncGenerator[ResponsesAgentStreamEvent, None]:
    """Streaming invocation handler registered with MLflow AgentServer."""
    ws, config, tools, messages, variables = _prepare_request_setup(request)
    selections = parse_selections(request.custom_inputs)

    async with managed_mcp_servers(config, ws, selections) as mcp:
        prompt_manuals, synthetic_tools = await _build_consumption_layer(
            request,
            mcp.servers,
            mcp.request_cache,
        )
        agent_tools, skills = await _request_skills(config, ws, request, tools, selections)
        layers = await prepare_context_layers(config, session_id=_conversation_id(request))
        agent = create_agent(
            config,
            tools=agent_tools,
            mcp_servers=mcp.servers,
            prompt_manuals=prompt_manuals,
            data_universe=await ensure_data_universe(ws, config, variables),
            synthetic_tools=synthetic_tools,
            gateway_skills=skills,
            memory=layers.memory,
            plan=layers.plan,
            history_search=prepare_history_search(_conversation_id(request), get_forwarded_headers()),
            result_reader=prepare_result_reader(mcp.servers),
            unavailable_mcp=mcp.unavailable,
        )
        if notice := user_notice(mcp.unavailable):
            yield build_message_event(notice)
        async for event in _run_stream(agent, messages, config["MAX_ITERATIONS"], request, config):
            yield event


async def _interactive() -> None:
    """Simple REPL for local testing.

    Skips the prompt-manual fetch entirely -- there is no
    ``request.context`` to derive a conversation_id from and the
    interactive loop is for quick smoke-testing, not realistic
    request shapes.
    """
    ws = get_workspace_client()
    config, variables = load_agent_config_and_vars(ws)
    tools = build_all_tools(ws, config, variables)

    async with managed_mcp_servers(config, ws) as mcp:
        agent = create_agent(
            config,
            tools=tools,
            mcp_servers=mcp.servers,
            data_universe=await ensure_data_universe(ws, config, variables),
            gateway_skills=await prepare_gateway_skills(config, ws, get_user_workspace_client(), Selections()),
            unavailable_mcp=mcp.unavailable,
        )
        n_tools = len(tools) + len(mcp.servers)
        if notice := user_notice(mcp.unavailable):
            print(notice)
        print(f"Agent ready with {n_tools} tool source(s). Type 'exit' to quit.\n")
        while True:
            user_input = input("You: ").strip()
            if user_input.lower() in {"exit", "quit", ""}:
                break
            with replay_scope():
                result = await Runner.run(
                    starting_agent=agent,
                    input=user_input,
                    max_turns=config["MAX_ITERATIONS"],
                    run_config=run_config_for(config),
                    context=new_request_state(),
                )
            print(f"Agent: {result.final_output}\n")


if __name__ == "__main__":
    asyncio.run(_interactive())
