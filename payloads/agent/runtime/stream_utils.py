"""
Stream Event Processing
========================
Translates OpenAI Agents SDK ``stream_events()`` into the
``ResponsesAgentStreamEvent`` dicts expected by the MLflow
AgentServer (Apps) and ``ResponsesAgent`` (Model Serving).

Ported from the official Databricks ``agent-openai-agents-sdk``
template (``agent_server/utils.py``).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, AsyncGenerator, AsyncIterator, cast
from uuid import uuid4

from agents.items import ItemHelpers
from agents.lifecycle import RunHooksBase
from agents.run_context import RunContextWrapper
from agents.stream_events import StreamEvent
from agents.tool import Tool
from mlflow.types.responses import ResponsesAgentStreamEvent
from openai.types.responses import ResponseFunctionToolCall

from payloads.agent.runtime.tool_output_renderers import render_extra_text_part


@dataclass(frozen=True)
class EarlyToolOutput:
    """A tool result that finished before the SDK's batched ``tool_output`` event."""

    call_id: str
    output: Any
    raw_item: dict[str, Any]
    name: str | None
    arguments: Any


class EarlyToolOutputHooks(RunHooksBase[Any, Any]):
    """Push each completed function-tool result onto *queue* as it finishes.

    The Agents SDK (0.15.x) only streams a step's ``tool_output`` events after
    every parallel call in that step has returned.  The chat UI therefore
    marks every tool done at the slowest call's timestamp.  These hooks do
    not change the agent loop; they only surface completion earlier on the
    SSE stream (#687).
    """

    def __init__(self, queue: asyncio.Queue[EarlyToolOutput]) -> None:
        self._queue = queue

    async def on_tool_end(
        self,
        context: RunContextWrapper[Any],
        agent: Any,
        tool: Tool,
        result: Any,
    ) -> None:
        call_id = getattr(context, "tool_call_id", None)
        if not isinstance(call_id, str) or not call_id:
            return
        name = getattr(context, "tool_name", None)
        if not isinstance(name, str) or not name:
            name = getattr(tool, "name", None)
        arguments = getattr(context, "tool_arguments", None)
        tool_call = getattr(context, "tool_call", None)
        self._queue.put_nowait(
            EarlyToolOutput(
                call_id=call_id,
                output=result,
                raw_item=_function_call_output_item(call_id, result, tool_call),
                name=name if isinstance(name, str) else None,
                arguments=arguments,
            )
        )


def start_early_tool_outputs() -> tuple[EarlyToolOutputHooks, asyncio.Queue[EarlyToolOutput]]:
    """Return a per-request hook + queue pair for streamed runs."""
    queue: asyncio.Queue[EarlyToolOutput] = asyncio.Queue()
    return EarlyToolOutputHooks(queue), queue


def _tool_call_meta(raw_item: Any) -> tuple[str | None, str | None, Any]:
    """Extract ``(call_id, tool_name, arguments)`` from a SDK tool-call
    raw item.

    The raw item may be either a Pydantic model
    (``ResponseFunctionToolCall`` etc.) or a dict.  Returns ``(None,
    None, None)`` for shapes we don't recognise.
    """
    if raw_item is None:
        return None, None, None
    getter = raw_item.get if isinstance(raw_item, dict) else lambda key, default=None: getattr(raw_item, key, default)
    call_id = getter("call_id") or getter("id")
    name = getter("name")
    arguments = getter("arguments")
    return call_id, name, arguments


def _function_call_output_item(call_id: str, output: Any, tool_call: Any) -> dict[str, Any]:
    """Match the SDK's persisted ``function_call_output`` item shape."""
    if not isinstance(tool_call, ResponseFunctionToolCall):
        tool_call = ResponseFunctionToolCall(
            call_id=call_id,
            name="",
            arguments="",
            type="function_call",
        )
    item = ItemHelpers.tool_call_output_item(tool_call, output)
    if isinstance(item, dict):
        return dict(item)
    return {"call_id": call_id, "output": str(output), "type": "function_call_output"}


def _tool_output_events(
    *,
    call_id: str | None,
    input_item: Any,
    output: Any,
    name: str | None,
    arguments: Any,
    pending_calls: dict[str, tuple[str, Any]],
    emitted_call_ids: set[str],
) -> list[ResponsesAgentStreamEvent]:
    """One ``function_call_output`` item, plus any extra renderer message."""
    if call_id and call_id in emitted_call_ids:
        return []
    if call_id:
        emitted_call_ids.add(call_id)
        cached = pending_calls.pop(call_id, None)
        if name is None and cached is not None:
            name, arguments = cached
    events = [
        ResponsesAgentStreamEvent(
            type="response.output_item.done",
            item=input_item,
        )
    ]
    if name:
        extra = render_extra_text_part(name, arguments, output)
        if extra:
            events.append(build_message_event(extra))
    return events


def _early_output_events(
    early: EarlyToolOutput,
    pending_calls: dict[str, tuple[str, Any]],
    emitted_call_ids: set[str],
) -> list[ResponsesAgentStreamEvent]:
    return _tool_output_events(
        call_id=early.call_id,
        input_item=early.raw_item,
        output=early.output,
        name=early.name,
        arguments=early.arguments,
        pending_calls=pending_calls,
        emitted_call_ids=emitted_call_ids,
    )


def _streamed_call_id(event_type: str, item: Any) -> str | None:
    """The ``call_id`` of a ``function_call`` whose ``done`` event this is, else ``None``."""
    if event_type != "response.output_item.done" or not isinstance(item, dict):
        return None
    call_id = item.get("call_id")
    return call_id if item.get("type") == "function_call" and isinstance(call_id, str) else None


async def _anext_or_none(stream: AsyncIterator[StreamEvent]) -> StreamEvent | None:
    try:
        return await stream.__anext__()
    except StopAsyncIteration:
        return None


async def merge_stream_with_early_outputs(
    sdk_stream: AsyncIterator[StreamEvent],
    early_outputs: asyncio.Queue[EarlyToolOutput],
) -> AsyncIterator[StreamEvent | EarlyToolOutput]:
    """Yield SDK events and per-tool completions as either source produces them."""
    sdk_task: asyncio.Task[StreamEvent | None] = asyncio.create_task(_anext_or_none(sdk_stream))
    early_task: asyncio.Task[EarlyToolOutput] | None = asyncio.create_task(early_outputs.get())
    try:
        while True:
            wait_for: list[asyncio.Task[Any]] = [sdk_task]
            if early_task is not None:
                wait_for.append(early_task)
            done, _ = await asyncio.wait(wait_for, return_when=asyncio.FIRST_COMPLETED)
            if early_task is not None and early_task in done:
                yield early_task.result()
                early_task = asyncio.create_task(early_outputs.get())
            if sdk_task not in done:
                continue
            event = sdk_task.result()
            if event is None:
                break
            yield event
            sdk_task = asyncio.create_task(_anext_or_none(sdk_stream))
        if early_task is not None:
            if early_task.done() and not early_task.cancelled():
                yield early_task.result()
            else:
                early_task.cancel()
                try:
                    await early_task
                except (asyncio.CancelledError, Exception):
                    pass
            early_task = None
        while True:
            try:
                yield early_outputs.get_nowait()
            except asyncio.QueueEmpty:
                break
    finally:
        if not sdk_task.done():
            sdk_task.cancel()
        if early_task is not None and not early_task.done():
            early_task.cancel()


def build_message_item(text: str) -> dict[str, Any]:
    """Return a synthetic assistant ``message`` output item carrying *text*.

    Shared by the streaming and non-streaming paths so a runtime-authored
    message has one shape: the ``@invoke`` handler puts it straight into
    ``ResponsesAgentResponse.output``, while :func:`build_message_event`
    wraps it for the ``@stream`` handler.
    """
    return {
        "id": str(uuid4()),
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [
            {
                "type": "output_text",
                "text": text,
                "annotations": [],
            },
        ],
    }


def build_message_event(
    text: str,
) -> ResponsesAgentStreamEvent:
    """Wrap *text* in a synthetic ``message`` output item event.

    The chat UI's part transformers render ``message`` items as
    Streamdown markdown, which is where the fence-dispatcher (see
    ``payloads/agent/ui/overrides/client/src/components/elements/response.tsx``)
    picks them up.
    """
    return ResponsesAgentStreamEvent(
        type="response.output_item.done",
        item=build_message_item(text),
    )


def _event_family(event_type: str, item: Any) -> str | None:
    """Which kind of output item a raw stream event belongs to.

    Paired with ``output_index`` this identifies the item an event refers
    to.  ``output_index`` alone is not enough: the SDK gives the message
    index 0 when its text starts before any reasoning, which is also the
    index reasoning always gets.
    """
    if isinstance(item, dict):
        item_type = item.get("type")
        return item_type if item_type in {"reasoning", "message", "function_call"} else None
    if event_type.startswith("response.reasoning"):
        return "reasoning"
    if event_type.startswith(("response.output_text", "response.content_part", "response.refusal")):
        return "message"
    if event_type.startswith("response.function_call_arguments"):
        return "function_call"
    return None


def _strip_reasoning_from_completed(event_data: dict[str, Any]) -> None:
    """Drop reasoning items from a ``response.completed`` event's output."""
    response = event_data.get("response")
    if not isinstance(response, dict) or not isinstance(response.get("output"), list):
        return
    response["output"] = [
        item for item in response["output"] if not (isinstance(item, dict) and item.get("type") == "reasoning")
    ]


async def process_agent_stream_events(
    async_stream: AsyncIterator[StreamEvent],
    *,
    emit_reasoning: bool = True,
    early_tool_outputs: asyncio.Queue[EarlyToolOutput] | None = None,
) -> AsyncGenerator[ResponsesAgentStreamEvent, None]:
    """Yield ``ResponsesAgentStreamEvent`` items from a Runner stream.

    Handles two SDK event families:

    ``raw_response_event``
        LLM-level events (text deltas, output_item.added, etc.).
        Each new output item gets a fresh ``item_id`` so downstream
        consumers can correlate deltas with their ``done`` event.
        The SDK emits every ``*.done`` event after the last item was
        added, so ids are tracked per item -- ``(output_index, family)``
        within one model response -- not as "the latest item".  The chat
        UI de-duplicates a reasoning ``done`` against its streamed deltas
        by id; a mismatched id renders the reasoning twice.

    ``run_item_stream_event``
        - ``tool_call_item``: cached by ``call_id`` so the matching
          output can be passed to a tool-output renderer (#122).
        - ``tool_call_output_item``: forwarded as
          ``response.output_item.done`` so the AgentServer / AI
          Playground can display intermediate tool results, then
          optionally followed by an extra ``message`` item carrying
          rich-rendering fences from
          :func:`payloads.agent.runtime.tool_output_renderers.render_extra_text_part`.

    *early_tool_outputs* is the per-request queue filled by
    :class:`EarlyToolOutputHooks`.  Completions are forwarded as soon as
    each tool returns; the SDK's later ``tool_call_output_item`` for the
    same ``call_id`` is skipped so the UI sees the result once (#687).  A
    completion is held until its ``function_call`` has been streamed (or the
    SDK's own output for it arrives): a fast tool can return while that
    ``done`` event is still queued, and the chat UI's provider rejects a
    tool result that precedes its call.

    With *emit_reasoning* off, every reasoning event -- and any reasoning
    item inside ``response.completed`` -- is dropped, for callers that
    only want the answer.
    """
    curr_item_id = str(uuid4())
    item_ids: dict[tuple[int, str], str] = {}
    # Fallback when an index moves under an item: the SDK re-numbers the
    # message once reasoning arrives after its text has started, so the
    # message's ``done`` event no longer matches the index it was added at.
    latest_by_family: dict[str, str] = {}
    pending_calls: dict[str, tuple[str, Any]] = {}
    emitted_call_ids: set[str] = set()
    streamed_call_ids: set[str] = set()
    held_outputs: dict[str, EarlyToolOutput] = {}
    merged: AsyncIterator[StreamEvent | EarlyToolOutput] = (
        merge_stream_with_early_outputs(async_stream, early_tool_outputs)
        if early_tool_outputs is not None
        else async_stream
    )

    async for event in merged:
        if isinstance(event, EarlyToolOutput):
            if event.call_id not in streamed_call_ids:
                held_outputs[event.call_id] = event
                continue
            for stream_event in _early_output_events(event, pending_calls, emitted_call_ids):
                yield stream_event
            continue

        if event.type == "raw_response_event":
            event_data = event.data.model_dump()
            event_type = event_data["type"]
            item = event_data.get("item")
            family = _event_family(event_type, item)

            if event_type == "response.created":
                # ``output_index`` restarts at 0 for every model call.
                item_ids.clear()
                latest_by_family.clear()
            if not emit_reasoning:
                if family == "reasoning":
                    continue
                if event_type == "response.completed":
                    _strip_reasoning_from_completed(event_data)

            output_index = event_data.get("output_index")
            key = (int(output_index), family) if isinstance(output_index, int) and family else None

            if event_type == "response.output_item.added":
                curr_item_id = str(uuid4())
                if key is not None:
                    item_ids[key] = curr_item_id
                    latest_by_family[key[1]] = curr_item_id
                event_data["item"]["id"] = curr_item_id
            else:
                item_id = item_ids.get(key) if key is not None else None
                if item_id is None and family is not None:
                    item_id = latest_by_family.get(family)
                if item_id is None:
                    item_id = curr_item_id
                if isinstance(item, dict) and item.get("id") is not None:
                    item["id"] = item_id
                elif event_data.get("item_id") is not None:
                    event_data["item_id"] = item_id

            # ``event_data`` is the duck-typed dict shape the MLflow
            # AgentServer / ResponsesAgent consumers already accept;
            # cast preserves the historic behaviour (no model
            # round-trip) while satisfying the declared return type.
            yield cast(ResponsesAgentStreamEvent, event_data)

            call_id = _streamed_call_id(event_type, item)
            if call_id is not None:
                streamed_call_ids.add(call_id)
                held = held_outputs.pop(call_id, None)
                if held is not None:
                    for stream_event in _early_output_events(held, pending_calls, emitted_call_ids):
                        yield stream_event

        elif event.type == "run_item_stream_event":
            if event.item.type == "tool_call_item":
                call_id, name, arguments = _tool_call_meta(event.item.raw_item)
                if call_id and name:
                    pending_calls[call_id] = (name, arguments)

            elif event.item.type == "tool_call_output_item":
                input_item = event.item.to_input_item()
                raw_call_id = input_item.get("call_id") if isinstance(input_item, dict) else None
                call_id = raw_call_id if isinstance(raw_call_id, str) else None
                held = held_outputs.pop(call_id, None) if call_id else None
                if held is not None:
                    for stream_event in _early_output_events(held, pending_calls, emitted_call_ids):
                        yield stream_event
                    continue
                for stream_event in _tool_output_events(
                    call_id=call_id,
                    input_item=input_item,
                    output=event.item.output,
                    name=None,
                    arguments=None,
                    pending_calls=pending_calls,
                    emitted_call_ids=emitted_call_ids,
                ):
                    yield stream_event
