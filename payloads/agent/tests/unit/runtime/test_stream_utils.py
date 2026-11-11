"""Tests for payloads/agent/runtime/stream_utils.py -- event construction."""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any, cast

import pytest
from agents.models.chatcmpl_stream_handler import ChatCmplStreamHandler
from agents.stream_events import RawResponsesStreamEvent, RunItemStreamEvent
from openai._models import construct_type
from openai.types.chat import ChatCompletionChunk
from openai.types.responses import Response

from payloads.agent.runtime import tool_output_renderers as tor
from payloads.agent.runtime.stream_utils import (
    EarlyToolOutput,
    build_message_event,
    build_message_item,
    process_agent_stream_events,
    start_early_tool_outputs,
)
from payloads.agent.runtime.tool_output_renderers import register


def test_message_item_shape() -> None:
    """The chat UI renders ``message`` items with ``output_text`` content
    parts; a wrong shape means the text silently never appears, which is
    the worst outcome for the fail-soft notice that uses this.
    """
    item = build_message_item("hello")
    assert item["type"] == "message"
    assert item["role"] == "assistant"
    assert item["status"] == "completed"
    assert item["content"] == [{"type": "output_text", "text": "hello", "annotations": []}]
    assert item["id"]


def test_message_items_get_distinct_ids() -> None:
    assert build_message_item("a")["id"] != build_message_item("a")["id"]


def test_message_event_wraps_the_item() -> None:
    event = build_message_event("hello")
    assert event.type == "response.output_item.done"
    item = getattr(event, "item")
    assert item["content"][0]["text"] == "hello"


# ---------------------------------------------------------------------------
# process_agent_stream_events -- item ids and reasoning emission
# ---------------------------------------------------------------------------
# Events come from the real SDK stream handler, so ``output_index`` and the
# order of the ``*.done`` events are what production sees.  Reasoning is fed
# as ``delta.reasoning_content`` -- the shape ``reasoning.NormalizedStream``
# turns Databricks list content into.

MODEL = "databricks-claude-sonnet-5-5"


def _chunk(delta: dict[str, Any], *, finish_reason: str | None = None) -> ChatCompletionChunk:
    payload = {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": MODEL,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return cast(ChatCompletionChunk, construct_type(type_=ChatCompletionChunk, value=payload))


def _tool_delta(index: int, call_id: str) -> dict[str, Any]:
    return {
        "tool_calls": [
            {"index": index, "id": call_id, "type": "function", "function": {"name": "lookup", "arguments": "{}"}}
        ]
    }


async def _model_events(chunks: list[ChatCompletionChunk]) -> list[Any]:
    async def stream() -> AsyncIterator[ChatCompletionChunk]:
        for chunk in chunks:
            yield chunk

    response = Response(
        id="__fake_id__",
        created_at=time.time(),
        model=MODEL,
        object="response",
        output=[],
        tool_choice="auto",
        tools=[],
        parallel_tool_calls=False,
    )
    return [event async for event in ChatCmplStreamHandler.handle_stream(response, cast(Any, stream()), model=MODEL)]


async def _process(sdk_events: list[Any], *, emit_reasoning: bool = True) -> list[dict[str, Any]]:
    async def stream() -> AsyncIterator[Any]:
        for event in sdk_events:
            yield RawResponsesStreamEvent(data=event)

    return [
        cast(dict[str, Any], event)
        async for event in process_agent_stream_events(stream(), emit_reasoning=emit_reasoning)
    ]


def _reasoning_then_text_then_tools() -> list[ChatCompletionChunk]:
    return [
        _chunk({"role": "assistant", "reasoning_content": "Let me"}),
        _chunk({"reasoning_content": " check."}),
        _chunk({"content": "Looking"}),
        _chunk({"content": " it up."}),
        _chunk(_tool_delta(0, "toolu_1")),
        _chunk(_tool_delta(1, "toolu_2")),
        _chunk({}, finish_reason="tool_calls"),
    ]


def _ids(events: list[dict[str, Any]], *, family: str) -> set[str]:
    """Every id the processed *events* carry for one item family."""
    found: set[str] = set()
    for event in events:
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == family:
            found.add(item["id"])
        elif family == "reasoning" and event["type"].startswith("response.reasoning"):
            found.add(event["item_id"])
        elif family == "message" and event["type"].startswith(("response.output_text", "response.content_part")):
            found.add(event["item_id"])
    return found


@pytest.mark.asyncio
async def test_reasoning_deltas_and_done_share_one_id() -> None:
    """The chat UI de-duplicates the reasoning ``done`` by id; a mismatch shows it twice."""
    events = await _process(await _model_events(_reasoning_then_text_then_tools()))
    reasoning_ids = _ids(events, family="reasoning")
    assert len(reasoning_ids) == 1
    done = [e for e in events if e["type"] == "response.output_item.done" and e["item"]["type"] == "reasoning"]
    assert done and done[0]["item"]["id"] in reasoning_ids


@pytest.mark.asyncio
async def test_message_keeps_its_own_id_through_done() -> None:
    events = await _process(await _model_events(_reasoning_then_text_then_tools()))
    message_ids = _ids(events, family="message")
    assert len(message_ids) == 1
    assert message_ids.isdisjoint(_ids(events, family="reasoning"))


@pytest.mark.asyncio
async def test_parallel_function_calls_get_distinct_ids() -> None:
    events = await _process(await _model_events(_reasoning_then_text_then_tools()))
    by_call: dict[str, set[str]] = {}
    for event in events:
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "function_call":
            by_call.setdefault(item["call_id"], set()).add(item["id"])
    assert set(by_call) == {"toolu_1", "toolu_2"}
    assert all(len(ids) == 1 for ids in by_call.values())
    assert by_call["toolu_1"] != by_call["toolu_2"]


@pytest.mark.asyncio
async def test_text_before_reasoning_does_not_collide() -> None:
    """Both items sit at ``output_index`` 0 when text opens first."""
    chunks = [
        _chunk({"role": "assistant", "content": "Hi."}),
        _chunk({"reasoning_content": "Hmm."}),
        _chunk({}, finish_reason="stop"),
    ]
    events = await _process(await _model_events(chunks))
    reasoning_ids = _ids(events, family="reasoning")
    message_ids = _ids(events, family="message")
    assert len(reasoning_ids) == 1
    assert len(message_ids) == 1
    assert reasoning_ids != message_ids


@pytest.mark.asyncio
async def test_each_model_call_gets_fresh_ids() -> None:
    first = await _model_events(_reasoning_then_text_then_tools())
    second = await _model_events(_reasoning_then_text_then_tools())
    events = await _process(first + second)
    assert len(_ids(events, family="reasoning")) == 2


@pytest.mark.asyncio
async def test_emission_off_drops_every_reasoning_event() -> None:
    events = await _process(await _model_events(_reasoning_then_text_then_tools()), emit_reasoning=False)
    assert not _ids(events, family="reasoning")
    completed = [e for e in events if e["type"] == "response.completed"]
    assert completed
    assert all(item["type"] != "reasoning" for item in completed[0]["response"]["output"])
    # Everything else still arrives.
    assert _ids(events, family="message")
    assert any(e["type"] == "response.output_item.done" and e["item"]["type"] == "function_call" for e in events)


def _as_event_dict(event: Any) -> dict[str, Any]:
    return event if isinstance(event, dict) else event.model_dump()


def _tool_call_event(call_id: str, name: str) -> RunItemStreamEvent:
    return RunItemStreamEvent(
        name="tool_called",
        item=cast(
            Any,
            SimpleNamespace(
                type="tool_call_item",
                raw_item={"call_id": call_id, "name": name, "arguments": "{}"},
            ),
        ),
    )


def _sdk_tool_output_event(call_id: str, output: str) -> RunItemStreamEvent:
    return RunItemStreamEvent(
        name="tool_output",
        item=cast(
            Any,
            SimpleNamespace(
                type="tool_call_output_item",
                output=output,
                to_input_item=lambda: {"type": "function_call_output", "call_id": call_id, "output": output},
            ),
        ),
    )


def _early(call_id: str, output: str, name: str | None = "lookup") -> EarlyToolOutput:
    return EarlyToolOutput(
        call_id=call_id,
        output=output,
        raw_item={"type": "function_call_output", "call_id": call_id, "output": output},
        name=name,
        arguments="{}",
    )


async def _process_mixed(
    sdk_events: list[Any],
    early_outputs: asyncio.Queue[EarlyToolOutput] | None = None,
) -> list[dict[str, Any]]:
    async def stream() -> AsyncIterator[Any]:
        for event in sdk_events:
            yield event

    return [
        _as_event_dict(event) async for event in process_agent_stream_events(stream(), early_tool_outputs=early_outputs)
    ]


def _function_call_outputs(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        event["item"]
        for event in events
        if event.get("type") == "response.output_item.done"
        and isinstance(event.get("item"), dict)
        and event["item"].get("type") == "function_call_output"
    ]


@pytest.mark.asyncio
async def test_early_tool_output_is_not_repeated_when_sdk_catches_up() -> None:
    queue: asyncio.Queue[EarlyToolOutput] = asyncio.Queue()
    queue.put_nowait(_early("c1", "fast"))
    events = await _process_mixed(
        [_tool_call_event("c1", "lookup"), _sdk_tool_output_event("c1", "fast")],
        early_outputs=queue,
    )
    outputs = _function_call_outputs(events)
    assert outputs == [{"type": "function_call_output", "call_id": "c1", "output": "fast"}]


@pytest.mark.asyncio
async def test_early_output_before_tool_call_item_still_renders_once(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, Any, Any]] = []

    def renderer(tool_name: str, arguments: Any, output: Any) -> str:
        seen.append((tool_name, arguments, output))
        return "extra-markdown\n\n"

    monkeypatch.setattr(tor, "_REGISTRATIONS", list(tor._REGISTRATIONS))
    register(re.compile(r"^lookup$"), renderer)
    queue: asyncio.Queue[EarlyToolOutput] = asyncio.Queue()
    queue.put_nowait(_early("c1", "fast", name="lookup"))
    events = await _process_mixed(
        [_sdk_tool_output_event("c1", "fast")],
        early_outputs=queue,
    )
    outputs = _function_call_outputs(events)
    extras = [
        event["item"]["content"][0]["text"]
        for event in events
        if event.get("type") == "response.output_item.done"
        and isinstance(event.get("item"), dict)
        and event["item"].get("type") == "message"
    ]
    assert outputs == [{"type": "function_call_output", "call_id": "c1", "output": "fast"}]
    assert extras == ["extra-markdown\n\n"]
    assert seen == [("lookup", "{}", "fast")]


def _done_item_types(events: list[dict[str, Any]]) -> list[tuple[str, str]]:
    return [
        (event["item"]["type"], event["item"].get("call_id", ""))
        for event in events
        if event.get("type") == "response.output_item.done" and isinstance(event.get("item"), dict)
    ]


@pytest.mark.asyncio
async def test_early_output_waits_for_its_function_call_to_stream() -> None:
    """A fast tool returning before its call's ``done`` event is streamed must not
    overtake it: the chat UI's provider rejects a result without a prior call."""
    model_events = await _model_events(
        [
            _chunk({"role": "assistant", "content": "Saving."}),
            _chunk(_tool_delta(0, "toolu_1")),
            _chunk({}, finish_reason="tool_calls"),
        ]
    )
    queue: asyncio.Queue[EarlyToolOutput] = asyncio.Queue()
    queue.put_nowait(_early("toolu_1", "fast"))
    events = await _process_mixed(
        [RawResponsesStreamEvent(data=event) for event in model_events]
        + [_tool_call_event("toolu_1", "lookup"), _sdk_tool_output_event("toolu_1", "fast")],
        early_outputs=queue,
    )
    calls = [kind for kind in _done_item_types(events) if kind[1] == "toolu_1"]
    assert calls == [("function_call", "toolu_1"), ("function_call_output", "toolu_1")]


@pytest.mark.asyncio
async def test_parallel_tools_mark_done_in_completion_order() -> None:
    """Acceptance for #687: three staggered tools finish in the UI as they return."""
    import httpx
    from agents import Agent, OpenAIChatCompletionsModel, Runner, function_tool
    from openai import AsyncOpenAI

    from payloads.agent.runtime.prompt_cache import PromptCachedCompletions
    from payloads.agent.runtime.reasoning import replay_scope

    delays = {"fast": 0.05, "mid": 0.18, "slow": 0.32}

    @function_tool
    async def fast() -> str:
        """Return quickly."""
        await asyncio.sleep(delays["fast"])
        return "fast-result"

    @function_tool
    async def mid() -> str:
        """Return after a medium delay."""
        await asyncio.sleep(delays["mid"])
        return "mid-result"

    @function_tool
    async def slow() -> str:
        """Return last."""
        await asyncio.sleep(delays["slow"])
        return "slow-result"

    def sse_chunk(delta: dict[str, Any], *, finish_reason: str | None = None) -> dict[str, Any]:
        return {
            "id": "chatcmpl-1",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": MODEL,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }

    def sse(chunks: list[dict[str, Any]]) -> bytes:
        return "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks).encode() + b"data: [DONE]\n\n"

    tool_turn = sse(
        [
            sse_chunk({"role": "assistant", "content": "Looking them up."}),
            sse_chunk(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_fast",
                            "type": "function",
                            "function": {"name": "fast", "arguments": "{}"},
                        }
                    ]
                }
            ),
            sse_chunk(
                {
                    "tool_calls": [
                        {
                            "index": 1,
                            "id": "call_mid",
                            "type": "function",
                            "function": {"name": "mid", "arguments": "{}"},
                        }
                    ]
                }
            ),
            sse_chunk(
                {
                    "tool_calls": [
                        {
                            "index": 2,
                            "id": "call_slow",
                            "type": "function",
                            "function": {"name": "slow", "arguments": "{}"},
                        }
                    ]
                }
            ),
            sse_chunk({}, finish_reason="tool_calls"),
        ]
    )
    answer_turn = sse(
        [
            sse_chunk({"role": "assistant", "content": "Done."}),
            sse_chunk({}, finish_reason="stop"),
        ]
    )
    pending = iter([tool_turn, answer_turn])
    seen_requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(json.loads(request.content))
        return httpx.Response(200, content=next(pending), headers={"content-type": "text/event-stream"})

    client = AsyncOpenAI(
        api_key="test",
        base_url="https://example.invalid/serving-endpoints",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    cast(Any, client.chat).completions = PromptCachedCompletions(client=client)
    agent = Agent(
        name="Supervisor",
        instructions="Call the tools.",
        model=OpenAIChatCompletionsModel(model=MODEL, openai_client=client),
        tools=[fast, mid, slow],
    )
    hooks, early_tool_outputs = start_early_tool_outputs()
    with replay_scope():
        result = Runner.run_streamed(agent, "Run them.", hooks=hooks)

    finished: list[tuple[float, str, str]] = []
    async for event in process_agent_stream_events(
        result.stream_events(),
        early_tool_outputs=early_tool_outputs,
    ):
        payload = _as_event_dict(event)
        item = payload.get("item")
        if (
            payload.get("type") == "response.output_item.done"
            and isinstance(item, dict)
            and item.get("type") == "function_call_output"
        ):
            finished.append((time.monotonic(), item["call_id"], item["output"]))

    assert [call_id for _, call_id, _ in finished] == ["call_fast", "call_mid", "call_slow"]
    assert [output for _, _, output in finished] == ["fast-result", "mid-result", "slow-result"]
    assert finished[1][0] - finished[0][0] >= 0.08
    assert finished[2][0] - finished[1][0] >= 0.08
    assert result.final_output == "Done."
    follow_up = next(message for message in seen_requests[1]["messages"] if message["role"] == "assistant")
    assert [tool_call["id"] for tool_call in follow_up["tool_calls"]] == ["call_fast", "call_mid", "call_slow"]
