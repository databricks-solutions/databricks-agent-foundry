"""Tests for payloads/agent/runtime/reasoning.py -- thinking-model content.

The payloads follow the documented Databricks shape (``content`` as a
list of ``reasoning`` / ``text`` blocks).  They go through the real
Agents SDK converter and stream handler, because the point of the
normalisation is what *those* produce from it.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from typing import Any, cast

import pytest
from agents.models.chatcmpl_converter import Converter
from agents.models.chatcmpl_stream_handler import ChatCmplStreamHandler
from openai._models import construct_type
from openai.types.chat import ChatCompletion, ChatCompletionChunk
from openai.types.responses import Response, ResponseFunctionToolCall, ResponseOutputMessage, ResponseReasoningItem

# Imported at collection, before the session fixture in
# ``agent_foundry.pytest_plugin`` swaps ``databricks.sdk.WorkspaceClient``
# for a function: ``databricks_openai`` evaluates ``WorkspaceClient | None``
# when it is first imported, which fails against the stand-in.
from payloads.agent.runtime.content_filter import CONTENT_FILTER_NOTICE
from payloads.agent.runtime.prompt_cache import PromptCachedCompletions
from payloads.agent.runtime.reasoning import (
    MAX_REPLAY_ENTRIES,
    REASONING_REPLAY_KEY,
    NormalizedStream,
    ReasoningReplayStore,
    ReplayEntry,
    current_replay_store,
    inject_reasoning,
    normalize_completion,
    parse_replay_entries,
    regroup_parallel_calls,
    replay_outputs,
    replay_scope,
    resolve_emit_reasoning,
    split_content,
)

MODEL = "databricks-claude-sonnet-5-5"


def _reasoning(text: str, signature: str | None = "sig-1") -> dict[str, Any]:
    entry: dict[str, Any] = {"type": "summary_text", "text": text}
    if signature is not None:
        entry["signature"] = signature
    return {"type": "reasoning", "summary": [entry]}


def _tool_call(call_id: str, name: str = "lookup") -> dict[str, Any]:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": "{}"}}


def _stream_tool_call(call_id: str, name: str = "lookup") -> dict[str, Any]:
    return {"index": 0, **_tool_call(call_id, name)}


def _completion(
    content: Any,
    *,
    tool_calls: list[dict[str, Any]] | None = None,
    finish_reason: str = "stop",
) -> ChatCompletion:
    """Parse a payload the way the openai client does -- without validation.

    That is how a list ``content`` survives to the SDK in production.
    """
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    payload = {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 0,
        "model": MODEL,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
    }
    return cast(ChatCompletion, construct_type(type_=ChatCompletion, value=payload))


def _chunk(delta: dict[str, Any], *, finish_reason: str | None = None) -> ChatCompletionChunk:
    payload = {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": MODEL,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return cast(ChatCompletionChunk, construct_type(type_=ChatCompletionChunk, value=payload))


def _usage_chunk() -> ChatCompletionChunk:
    payload = {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": MODEL,
        "choices": [],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }
    return cast(ChatCompletionChunk, construct_type(type_=ChatCompletionChunk, value=payload))


class _FakeUpstream:
    """An ``AsyncStream`` stand-in: iterable once, records ``close``."""

    def __init__(self, chunks: list[ChatCompletionChunk]) -> None:
        self._chunks = chunks
        self.closed = False
        self.response = "upstream-response"

    def __aiter__(self) -> AsyncIterator[ChatCompletionChunk]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[ChatCompletionChunk]:
        for chunk in self._chunks:
            yield chunk

    async def close(self) -> None:
        self.closed = True


def _tool_stream() -> list[ChatCompletionChunk]:
    """Reasoning in fragments, signature last, then text, then a tool call."""
    return [
        _chunk({"role": "assistant", "content": [_reasoning("Let me", signature=None)]}),
        _chunk({"content": [_reasoning(" check.", signature=None)]}),
        _chunk({"content": [_reasoning("", signature="sig-1")]}),
        _chunk({"content": [{"type": "text", "text": "Looking"}]}),
        _chunk({"content": " it up."}),
        _chunk(
            {
                "tool_calls": [
                    {"index": 0, "id": "toolu_1", "type": "function", "function": {"name": "lookup", "arguments": ""}}
                ]
            }
        ),
        _chunk({"tool_calls": [{"index": 0, "function": {"arguments": "{}"}}]}),
        _chunk({}, finish_reason="tool_calls"),
        _usage_chunk(),
    ]


async def _drain(stream: NormalizedStream) -> list[Any]:
    return [chunk async for chunk in stream]


async def _sdk_events(chunks: list[ChatCompletionChunk], store: ReasoningReplayStore) -> list[Any]:
    """Run *chunks* through ``NormalizedStream`` and the SDK stream handler."""
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
    stream = NormalizedStream(_FakeUpstream(chunks), model=MODEL, store=store)
    return [event async for event in ChatCmplStreamHandler.handle_stream(response, cast(Any, stream), model=MODEL)]


class TestSplitContent:
    def test_text_and_reasoning_are_separated(self) -> None:
        split = split_content([_reasoning("Thinking."), {"type": "text", "text": "Answer."}])
        assert split.text == "Answer."
        assert split.summary == "Thinking."
        assert split.replayable is True

    def test_text_blocks_join_without_a_separator(self) -> None:
        split = split_content([{"type": "text", "text": "Hel"}, {"type": "text", "text": "lo"}])
        assert split.text == "Hello"
        assert split.summary is None
        assert split.replayable is False

    def test_reasoning_only_has_no_text(self) -> None:
        """``None``, not ``""``: the SDK opens a message item for any string."""
        assert split_content([_reasoning("Thinking.")]).text is None

    def test_summaries_of_separate_blocks_are_paragraphs(self) -> None:
        split = split_content([_reasoning("One."), _reasoning("Two.", signature="sig-2")])
        assert split.summary == "One.\n\nTwo."

    def test_encrypted_reasoning_is_replayable_but_has_no_summary(self) -> None:
        block = {"type": "reasoning", "summary": [{"type": "summary_encrypted_text", "data": "opaque"}]}
        split = split_content([block, {"type": "text", "text": "Answer."}])
        assert split.summary is None
        assert split.replayable is True

    def test_unsigned_reasoning_is_not_replayable(self) -> None:
        assert split_content([_reasoning("Thinking.", signature=None)]).replayable is False

    def test_unknown_blocks_are_ignored(self) -> None:
        split = split_content([{"type": "citation", "url": "x"}, {"type": "text", "text": "Answer."}])
        assert split.text == "Answer."


class TestNormalizeCompletion:
    def test_sdk_builds_a_reasoning_item_and_a_message(self) -> None:
        store = ReasoningReplayStore()
        completion = _completion([_reasoning("Thinking."), {"type": "text", "text": "Answer."}])

        normalize_completion(completion, model=MODEL, store=store)
        items = Converter.message_to_output_items(completion.choices[0].message)

        reasoning, message = items
        assert isinstance(reasoning, ResponseReasoningItem)
        assert [s.text for s in reasoning.summary] == ["Thinking."]
        # Signatures stay out of the SDK item, so they never reach callers.
        assert reasoning.encrypted_content is None
        assert isinstance(message, ResponseOutputMessage)
        assert message.content[0].text == "Answer."  # type: ignore[union-attr]

    def test_string_content_is_untouched(self) -> None:
        completion = _completion("Plain answer.")
        message = completion.choices[0].message

        normalize_completion(completion, model=MODEL, store=ReasoningReplayStore())

        assert message.content == "Plain answer."
        assert not hasattr(message, "reasoning_content")

    def test_text_only_list_becomes_a_string(self) -> None:
        completion = _completion([{"type": "text", "text": "Answer."}])
        normalize_completion(completion, model=MODEL, store=ReasoningReplayStore())
        assert completion.choices[0].message.content == "Answer."

    def test_reasoning_with_tool_calls_is_stored_for_replay(self) -> None:
        store = ReasoningReplayStore()
        original = [_reasoning("Need data."), {"type": "text", "text": "Checking."}]
        completion = _completion(list(original), tool_calls=[_tool_call("toolu_1"), _tool_call("toolu_2")])

        normalize_completion(completion, model=MODEL, store=store)
        items = Converter.message_to_output_items(completion.choices[0].message)

        assert [type(item) for item in items] == [
            ResponseReasoningItem,
            ResponseOutputMessage,
            ResponseFunctionToolCall,
            ResponseFunctionToolCall,
        ]
        for call_id in ("toolu_1", "toolu_2"):
            entry = store.get(MODEL, call_id)
            assert entry is not None
            assert entry.content == original

    def test_reasoning_only_tool_call_turn_has_no_message_item(self) -> None:
        completion = _completion([_reasoning("Need data.")], tool_calls=[_tool_call("toolu_1")])
        normalize_completion(completion, model=MODEL, store=ReasoningReplayStore())
        items = Converter.message_to_output_items(completion.choices[0].message)
        assert [type(item) for item in items] == [ResponseReasoningItem, ResponseFunctionToolCall]

    @pytest.mark.parametrize("blank", ["", "\n\n"])
    def test_blank_text_is_not_replayed(self, blank: str) -> None:
        store = ReasoningReplayStore()
        content = [_reasoning("Need data."), {"type": "text", "text": blank}]
        normalize_completion(_completion(content, tool_calls=[_tool_call("toolu_1")]), model=MODEL, store=store)
        entry = store.get(MODEL, "toolu_1")
        assert entry is not None
        assert entry.content == [_reasoning("Need data.")]

    def test_parallel_calls_without_reasoning_are_recorded_as_a_group(self) -> None:
        store = ReasoningReplayStore()
        completion = _completion("Checking.", tool_calls=[_tool_call("toolu_1"), _tool_call("toolu_2")])
        normalize_completion(completion, model=MODEL, store=store)
        assert [(entry.tool_call_ids, entry.content) for entry in store.produced()] == [(["toolu_1", "toolu_2"], [])]

    def test_final_answer_is_not_stored(self) -> None:
        store = ReasoningReplayStore()
        normalize_completion(
            _completion([_reasoning("Done."), {"type": "text", "text": "Hi."}]), model=MODEL, store=store
        )
        assert len(store) == 0

    def test_redacted_reasoning_is_stored_but_shows_nothing(self) -> None:
        store = ReasoningReplayStore()
        block = {"type": "reasoning", "summary": [{"type": "summary_encrypted_text", "data": "opaque"}]}
        completion = _completion([block], tool_calls=[_tool_call("toolu_1")])

        normalize_completion(completion, model=MODEL, store=store)
        items = Converter.message_to_output_items(completion.choices[0].message)

        assert [type(item) for item in items] == [ResponseFunctionToolCall]
        assert store.get(MODEL, "toolu_1") is not None

    def test_empty_answer_after_length_stop_is_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        completion = _completion([_reasoning("Long thought.")], finish_reason="length")
        with caplog.at_level(logging.WARNING, logger="payloads.agent.runtime.reasoning"):
            normalize_completion(completion, model=MODEL, store=ReasoningReplayStore())
        assert "output budget" in caplog.text

    def test_content_filter_on_empty_string_becomes_the_notice(self, caplog: pytest.LogCaptureFixture) -> None:
        completion = _completion("", finish_reason="content_filter")
        with caplog.at_level(logging.WARNING, logger="payloads.agent.runtime.reasoning"):
            normalize_completion(completion, model=MODEL, store=ReasoningReplayStore())
        assert completion.choices[0].message.content == CONTENT_FILTER_NOTICE
        assert "content_filter" in caplog.text

    def test_content_filter_does_not_replace_a_real_answer(self) -> None:
        completion = _completion("hello", finish_reason="content_filter")
        normalize_completion(completion, model=MODEL, store=ReasoningReplayStore())
        assert completion.choices[0].message.content == "hello"

    def test_stop_with_empty_text_is_not_a_filter_notice(self) -> None:
        completion = _completion("", finish_reason="stop")
        normalize_completion(completion, model=MODEL, store=ReasoningReplayStore())
        assert completion.choices[0].message.content == ""


class TestNormalizedStream:
    @pytest.mark.asyncio
    async def test_sdk_streams_reasoning_then_text(self) -> None:
        store = ReasoningReplayStore()
        events = await _sdk_events(_tool_stream(), store)
        types = [event.type for event in events]

        reasoning_deltas = [e.delta for e in events if e.type == "response.reasoning_summary_text.delta"]
        text_deltas = [e.delta for e in events if e.type == "response.output_text.delta"]
        assert "".join(reasoning_deltas) == "Let me check."
        assert "".join(text_deltas) == "Looking it up."
        # Reasoning opens before the message does.
        assert types.index("response.reasoning_summary_text.delta") < types.index("response.output_text.delta")

        completed = events[-1]
        assert completed.type == "response.completed"
        reasoning = completed.response.output[0]
        assert isinstance(reasoning, ResponseReasoningItem)
        assert reasoning.summary[0].text == "Let me check."
        assert reasoning.encrypted_content is None

    @pytest.mark.asyncio
    async def test_content_filter_stream_emits_the_notice(self) -> None:
        events = await _sdk_events(
            [
                _chunk({"role": "assistant", "content": ""}),
                _chunk({}, finish_reason="content_filter"),
            ],
            ReasoningReplayStore(),
        )
        text = "".join(e.delta for e in events if e.type == "response.output_text.delta")
        assert text == CONTENT_FILTER_NOTICE

    @pytest.mark.asyncio
    async def test_original_blocks_are_rebuilt_for_replay(self) -> None:
        store = ReasoningReplayStore()
        await _sdk_events(_tool_stream(), store)

        entry = store.get(MODEL, "toolu_1")
        assert entry is not None
        assert entry.content == [
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": "Let me check.", "signature": "sig-1"}]},
            {"type": "text", "text": "Looking it up."},
        ]

    @pytest.mark.asyncio
    async def test_reasoning_only_chunks_open_no_message(self) -> None:
        chunks = [
            _chunk({"role": "assistant", "content": [_reasoning("Thinking.")]}),
            _chunk(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "toolu_1",
                            "type": "function",
                            "function": {"name": "lookup", "arguments": "{}"},
                        }
                    ]
                }
            ),
            _chunk({}, finish_reason="tool_calls"),
        ]
        events = await _sdk_events(chunks, ReasoningReplayStore())
        added = [e.item.type for e in events if e.type == "response.output_item.added"]
        assert added == ["reasoning", "function_call"]

    @pytest.mark.asyncio
    async def test_plain_string_stream_passes_through(self) -> None:
        chunks = [_chunk({"role": "assistant", "content": "Hi"}), _chunk({"content": " there"}), _usage_chunk()]
        out = await _drain(NormalizedStream(_FakeUpstream(chunks), model=MODEL, store=ReasoningReplayStore()))
        assert out == chunks
        assert [c.choices[0].delta.content for c in out[:2]] == ["Hi", " there"]

    @pytest.mark.asyncio
    async def test_upstream_is_closed_and_attributes_delegate(self) -> None:
        upstream = _FakeUpstream([_chunk({"content": "x"})])
        stream = NormalizedStream(upstream, model=MODEL, store=ReasoningReplayStore())
        assert stream.response == "upstream-response"
        await _drain(stream)
        assert upstream.closed is True

    @pytest.mark.asyncio
    async def test_abandoned_stream_stores_nothing(self) -> None:
        store = ReasoningReplayStore()
        upstream = _FakeUpstream(_tool_stream())
        stream = NormalizedStream(upstream, model=MODEL, store=store)
        for _ in range(6):
            await anext(stream)
        await stream.aclose()
        assert upstream.closed is True
        assert len(store) == 0

    @pytest.mark.asyncio
    async def test_supports_async_with_and_anext_like_async_stream(self) -> None:
        upstream = _FakeUpstream([_chunk({"content": "x"}), _chunk({"content": "y"})])
        async with NormalizedStream(upstream, model=MODEL, store=ReasoningReplayStore()) as stream:
            first = await anext(stream)
        assert first.choices[0].delta.content == "x"
        assert upstream.closed is True

    @pytest.mark.asyncio
    async def test_empty_reasoning_fragments_add_no_block(self) -> None:
        store = ReasoningReplayStore()
        chunks = [
            _chunk({"role": "assistant", "content": [{"type": "reasoning", "summary": []}]}),
            _chunk({"content": [_reasoning("Let me", signature=None)]}),
            _chunk({"content": [{"type": "reasoning", "summary": []}]}),
            _chunk({"content": [_reasoning("", signature="sig-1")]}),
            _chunk({"tool_calls": [_stream_tool_call("toolu_1")]}),
            _chunk({}, finish_reason="tool_calls"),
        ]
        await _drain(NormalizedStream(_FakeUpstream(chunks), model=MODEL, store=store))
        entry = store.get(MODEL, "toolu_1")
        assert entry is not None
        assert entry.content == [_reasoning("Let me", signature="sig-1")]

    @pytest.mark.asyncio
    async def test_block_level_fields_on_a_later_fragment_are_kept(self) -> None:
        store = ReasoningReplayStore()
        chunks = [
            _chunk(
                {
                    "role": "assistant",
                    "content": [{"type": "reasoning", "summary": [{"type": "summary_text", "text": "Let me"}]}],
                }
            ),
            _chunk(
                {
                    "content": [
                        {
                            "type": "reasoning",
                            "signature": "blocksig",
                            "summary": [{"type": "summary_text", "text": ""}],
                        }
                    ]
                }
            ),
            _chunk({"tool_calls": [_stream_tool_call("toolu_1")]}),
            _chunk({}, finish_reason="tool_calls"),
        ]
        await _drain(NormalizedStream(_FakeUpstream(chunks), model=MODEL, store=store))
        entry = store.get(MODEL, "toolu_1")
        assert entry is not None, "a block-level signature makes the turn replayable, as it does non-streamed"
        assert entry.content == [
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": "Let me"}], "signature": "blocksig"}
        ]

    @pytest.mark.asyncio
    async def test_blank_text_is_not_replayed(self) -> None:
        """Anthropic rejects whitespace-only text blocks, so they are never stored."""
        store = ReasoningReplayStore()
        chunks = [
            _chunk({"role": "assistant", "content": [_reasoning("Need data.")]}),
            _chunk({"content": "\n\n"}),
            _chunk({"tool_calls": [_stream_tool_call("toolu_1")]}),
            _chunk({}, finish_reason="tool_calls"),
        ]
        await _drain(NormalizedStream(_FakeUpstream(chunks), model=MODEL, store=store))
        entry = store.get(MODEL, "toolu_1")
        assert entry is not None
        assert entry.content == [_reasoning("Need data.")]

    @pytest.mark.asyncio
    async def test_separate_blocks_read_the_same_on_both_transports(self) -> None:
        blocks = [
            _reasoning("A.", signature="sig-1"),
            _reasoning("B.", signature="sig-2"),
            {"type": "text", "text": "Hi"},
        ]
        chunks = [_chunk({"role": "assistant", "content": [block]}) for block in blocks]
        events = await _sdk_events(chunks, ReasoningReplayStore())
        streamed = "".join(e.delta for e in events if e.type == "response.reasoning_summary_text.delta")
        assert streamed == split_content(blocks).summary == "A.\n\nB."


class TestReplayStore:
    def test_model_mismatch_is_a_miss(self) -> None:
        store = ReasoningReplayStore()
        store.put(MODEL, ["toolu_1"], [_reasoning("x")])
        assert store.get("databricks-claude-opus-5-5", "toolu_1") is None

    def test_parallel_calls_each_resolve(self) -> None:
        store = ReasoningReplayStore()
        store.put(MODEL, ["toolu_a", "toolu_b"], [_reasoning("x")])
        assert store.get(MODEL, "toolu_a") is store.get(MODEL, "toolu_b") is not None

    def test_stored_content_is_a_copy(self) -> None:
        store = ReasoningReplayStore()
        content = [_reasoning("x")]
        store.put(MODEL, ["toolu_1"], content)
        content[0]["summary"][0]["text"] = "changed"
        entry = store.get(MODEL, "toolu_1")
        assert entry is not None
        assert entry.content[0]["summary"][0]["text"] == "x"

    def test_produced_lists_the_runs_own_entries_in_order(self) -> None:
        store = ReasoningReplayStore()
        store.put(MODEL, ["toolu_1"], [_reasoning("a")])
        store.put(MODEL, ["toolu_2", "toolu_3"], [_reasoning("b")])
        assert [entry.tool_call_ids for entry in store.produced()] == [["toolu_1"], ["toolu_2", "toolu_3"]]

    def test_seeded_entries_resolve_but_are_not_produced(self) -> None:
        store = ReasoningReplayStore()
        store.seed([_entry("toolu_1")])
        assert store.get(MODEL, "toolu_1") is not None
        assert store.produced() == []

    def test_unsigned_parallel_calls_are_produced_as_a_group_only(self) -> None:
        store = ReasoningReplayStore()
        store.put(MODEL, ["toolu_1", "toolu_2"], [])
        store.put(MODEL, ["toolu_3"], [])
        assert [(entry.tool_call_ids, entry.content) for entry in store.produced()] == [(["toolu_1", "toolu_2"], [])]
        assert store.get(MODEL, "toolu_1") is None

    def test_seeded_groups_are_never_replayed(self) -> None:
        store = ReasoningReplayStore()
        store.seed([ReplayEntry(model=MODEL, tool_call_ids=["toolu_1", "toolu_2"], content=[])])
        assert store.get(MODEL, "toolu_1") is None


def _entry(call_id: str = "toolu_1", content: list[dict[str, Any]] | None = None) -> ReplayEntry:
    blocks = content if content is not None else [_reasoning("Need data."), {"type": "text", "text": "Checking."}]
    return ReplayEntry(model=MODEL, tool_call_ids=[call_id], content=blocks)


class TestParseReplayEntries:
    def test_absent_key_is_not_an_opt_in(self) -> None:
        assert parse_replay_entries(None) is None
        assert parse_replay_entries({"other": 1}) is None

    def test_an_empty_list_opts_in(self) -> None:
        assert parse_replay_entries({REASONING_REPLAY_KEY: []}) == []

    def test_valid_entries_are_kept(self) -> None:
        raw = [_entry("toolu_1").model_dump(), _entry("toolu_2").model_dump()]
        entries = parse_replay_entries({REASONING_REPLAY_KEY: raw})
        assert entries is not None
        assert [entry.tool_call_ids for entry in entries] == [["toolu_1"], ["toolu_2"]]

    def test_a_non_list_is_ignored_with_a_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING):
            assert parse_replay_entries({REASONING_REPLAY_KEY: "x"}) == []
        assert "expected a list" in caplog.text

    @pytest.mark.parametrize(
        "item",
        [
            {"model": MODEL, "tool_call_ids": [], "content": [_reasoning("x")]},
            {"model": MODEL, "tool_call_ids": ["toolu_1"], "content": [_reasoning("x")], "extra": 1},
            {"model": "", "tool_call_ids": ["toolu_1"], "content": [_reasoning("x")]},
            "not an object",
        ],
    )
    def test_invalid_entries_are_skipped(self, item: Any) -> None:
        assert parse_replay_entries({REASONING_REPLAY_KEY: [item]}) == []

    @pytest.mark.parametrize(
        "content",
        [
            [_reasoning("x", signature=None), {"type": "text", "text": "t"}],
            [_reasoning("x"), {"type": "tool_use", "id": "toolu_9", "name": "n", "input": {}}],
            [_reasoning("x"), {"type": "image", "source": {"type": "url", "url": "https://example.invalid"}}],
        ],
        ids=["unsigned", "tool-use-block", "image-block"],
    )
    def test_only_signed_reasoning_and_text_is_seedable(self, content: list[dict[str, Any]]) -> None:
        raw = [_entry(content=content).model_dump()]
        assert parse_replay_entries({REASONING_REPLAY_KEY: raw}) == []

    def test_a_group_needs_two_calls(self) -> None:
        group = {"model": MODEL, "tool_call_ids": ["toolu_1", "toolu_2"], "content": []}
        single = {"model": MODEL, "tool_call_ids": ["toolu_3"], "content": []}
        entries = parse_replay_entries({REASONING_REPLAY_KEY: [group, single]})
        assert entries is not None
        assert [entry.tool_call_ids for entry in entries] == [["toolu_1", "toolu_2"]]

    def test_only_the_latest_entries_are_kept(self) -> None:
        raw = [_entry(f"toolu_{i}").model_dump() for i in range(MAX_REPLAY_ENTRIES + 2)]
        entries = parse_replay_entries({REASONING_REPLAY_KEY: raw})
        assert entries is not None
        assert len(entries) == MAX_REPLAY_ENTRIES
        assert entries[0].tool_call_ids == ["toolu_2"]


def _group(*call_ids: str) -> ReplayEntry:
    return _entry(call_ids[0]).model_copy(update={"tool_call_ids": list(call_ids)})


def _call(call_id: str) -> dict[str, Any]:
    return {"type": "function_call", "call_id": call_id, "name": "lookup", "arguments": "{}"}


def _output(call_id: str) -> dict[str, Any]:
    return {"type": "function_call_output", "call_id": call_id, "output": "42"}


class TestRegroupParallelCalls:
    def test_alternating_parallel_calls_become_calls_then_outputs(self) -> None:
        text = {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Checking."}]}
        items = [text, _call("a"), _output("a"), _call("b"), _output("b"), _call("c"), _output("c")]

        regrouped = regroup_parallel_calls(items, [_group("a"), _group("b", "c")])

        assert regrouped == [text, _call("a"), _output("a"), _call("b"), _call("c"), _output("b"), _output("c")]

    def test_already_grouped_calls_are_unchanged(self) -> None:
        items = [_call("a"), _call("b"), _output("a"), _output("b")]
        assert regroup_parallel_calls(items, [_group("a", "b")]) == items

    def test_calls_not_next_to_each_other_are_unchanged(self) -> None:
        reasoning = {"type": "reasoning", "summary": []}
        items = [_call("a"), _output("a"), reasoning, _call("b"), _output("b")]
        assert regroup_parallel_calls(items, [_group("a", "b")]) == items

    def test_without_entries_nothing_moves(self) -> None:
        items = [_call("a"), _output("a"), _call("b"), _output("b")]
        assert regroup_parallel_calls(items, []) == items


class TestReplayOutputs:
    def test_exports_the_produced_entries(self) -> None:
        store = ReasoningReplayStore()
        store.put(MODEL, ["toolu_1"], [_reasoning("x")])
        outputs = replay_outputs(store, export=True)
        assert outputs == {
            REASONING_REPLAY_KEY: [{"model": MODEL, "tool_call_ids": ["toolu_1"], "content": [_reasoning("x")]}]
        }

    def test_nothing_without_export_or_entries(self) -> None:
        store = ReasoningReplayStore()
        assert replay_outputs(store, export=True) is None
        store.put(MODEL, ["toolu_1"], [_reasoning("x")])
        assert replay_outputs(store, export=False) is None


class TestReplayScope:
    """Replay never outlives the agent run that produced the reasoning."""

    def _tool_turn(self, call_id: str = "toolu_1", text: str = "Need data.") -> ChatCompletion:
        return _completion(
            [_reasoning(text), {"type": "text", "text": "Checking."}],
            tool_calls=[_tool_call(call_id)],
        )

    def _replay_request(self, call_id: str = "toolu_1", content: Any = "Checking.") -> list[dict[str, Any]]:
        return [{"role": "assistant", "content": content, "tool_calls": [_tool_call(call_id)]}]

    def test_nothing_is_stored_or_replayed_outside_a_run(self) -> None:
        assert current_replay_store() is None
        normalize_completion(self._tool_turn(), model=MODEL)
        assert inject_reasoning(self._replay_request(), MODEL) == 0

    def test_a_run_replays_its_own_reasoning(self) -> None:
        with replay_scope() as store:
            normalize_completion(self._tool_turn(), model=MODEL)
            assert len(store) == 1
            assert inject_reasoning(self._replay_request(), MODEL) == 1
        assert current_replay_store() is None

    def test_one_run_never_sees_another_runs_reasoning(self) -> None:
        """A request naming another conversation's tool-call id gets nothing."""
        with replay_scope():
            normalize_completion(self._tool_turn(text="secret of user A"), model=MODEL)
        with replay_scope():
            messages = self._replay_request(content=None)
            assert inject_reasoning(messages, MODEL) == 0
            assert messages[0]["content"] is None

    @pytest.mark.asyncio
    async def test_concurrent_runs_are_isolated(self) -> None:
        import asyncio

        async def run(call_id: str) -> ReasoningReplayStore:
            with replay_scope() as store:
                normalize_completion(self._tool_turn(call_id), model=MODEL)
                await asyncio.sleep(0)
                return store

        first, second = await asyncio.gather(run("toolu_a"), run("toolu_b"))
        assert first.get(MODEL, "toolu_a") is not None and first.get(MODEL, "toolu_b") is None
        assert second.get(MODEL, "toolu_b") is not None and second.get(MODEL, "toolu_a") is None

    @pytest.mark.asyncio
    async def test_a_stream_keeps_the_store_of_the_run_that_created_it(self) -> None:
        with replay_scope() as store:
            stream = NormalizedStream(_FakeUpstream(_tool_stream()), model=MODEL)
        await _drain(stream)  # consumed after the block, as the run's task does
        assert store.get(MODEL, "toolu_1") is not None


class TestInjectReasoning:
    def _store(self) -> ReasoningReplayStore:
        store = ReasoningReplayStore()
        store.put(MODEL, ["toolu_1"], [_reasoning("Need data."), {"type": "text", "text": "Checking."}])
        return store

    def _messages(self, content: Any = "Checking.") -> list[dict[str, Any]]:
        return [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": content, "tool_calls": [_tool_call("toolu_1")]},
            {"role": "tool", "tool_call_id": "toolu_1", "content": "result"},
        ]

    def test_signed_content_is_put_back(self) -> None:
        messages = self._messages()
        assert inject_reasoning(messages, MODEL, self._store()) == 1
        assert messages[1]["content"] == [_reasoning("Need data."), {"type": "text", "text": "Checking."}]

    def test_reasoning_only_turn_matches_empty_content(self) -> None:
        store = ReasoningReplayStore()
        store.put(MODEL, ["toolu_1"], [_reasoning("Need data.")])
        for empty in (None, "", " "):
            messages = self._messages(content=empty)
            assert inject_reasoning(messages, MODEL, store) == 1
            assert messages[1]["content"] == [_reasoning("Need data.")]

    def test_changed_text_is_left_alone(self) -> None:
        messages = self._messages(content="Something else.")
        assert inject_reasoning(messages, MODEL, self._store()) == 0
        assert messages[1]["content"] == "Something else."

    def test_idempotent(self) -> None:
        store = self._store()
        messages = self._messages()
        inject_reasoning(messages, MODEL, store)
        once = [dict(m) for m in messages]
        assert inject_reasoning(messages, MODEL, store) == 0
        assert messages == once

    def test_cache_markers_never_reach_the_store(self) -> None:
        """Blocks are copied, so ``apply_cache_breakpoints`` marks the copy only."""
        store = self._store()
        messages = self._messages()
        inject_reasoning(messages, MODEL, store)
        messages[1]["content"][-1]["cache_control"] = {"type": "ephemeral"}
        entry = store.get(MODEL, "toolu_1")
        assert entry is not None
        assert "cache_control" not in entry.content[-1]

    def test_other_models_and_unknown_calls_are_skipped(self) -> None:
        store = self._store()
        assert inject_reasoning(self._messages(), "databricks-claude-sonnet-4-5", store) == 0
        unknown = self._messages()
        unknown[1]["tool_calls"] = [_tool_call("toolu_9")]
        assert inject_reasoning(unknown, MODEL, store) == 0


class TestResolveEmitReasoning:
    def test_defaults_to_on(self) -> None:
        assert resolve_emit_reasoning(None, None) is True
        assert resolve_emit_reasoning({}, {}) is True

    def test_spec_default_applies(self) -> None:
        assert resolve_emit_reasoning({"REASONING_EMIT": False}, None) is False

    def test_request_override_wins(self) -> None:
        assert resolve_emit_reasoning({"REASONING_EMIT": False}, {"include_reasoning": True}) is True
        assert resolve_emit_reasoning({"REASONING_EMIT": True}, {"include_reasoning": False}) is False

    @pytest.mark.parametrize(("value", "expected"), [("false", False), ("FALSE", False), (" true ", True)])
    def test_string_override_is_accepted(self, value: str, expected: bool) -> None:
        assert resolve_emit_reasoning({"REASONING_EMIT": not expected}, {"include_reasoning": value}) is expected

    @pytest.mark.parametrize("value", ["yes", 1])
    def test_non_bool_override_is_ignored_with_a_warning(self, value: Any, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level("WARNING", logger="payloads.agent.runtime.reasoning"):
            assert resolve_emit_reasoning({"REASONING_EMIT": False}, {"include_reasoning": value}) is False
        assert any("Ignoring custom_inputs.include_reasoning" in r.getMessage() for r in caplog.records)

    def test_no_override_logs_nothing(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level("WARNING", logger="payloads.agent.runtime.reasoning"):
            assert resolve_emit_reasoning(None, {"include_reasoning": None}) is True
            assert resolve_emit_reasoning(None, {}) is True
        assert not caplog.records


# ---------------------------------------------------------------------------
# A real Agents SDK run against a scripted endpoint
# ---------------------------------------------------------------------------
# The whole chain the issue is about: list content from the endpoint, the
# SDK's tool loop, and the signed reasoning back on the wire for the next
# step.  Only the HTTP transport is fake.


def _sse(chunks: list[dict[str, Any]]) -> bytes:
    import json

    return "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks).encode() + b"data: [DONE]\n\n"


def _sse_chunk(delta: dict[str, Any], finish_reason: str | None = None) -> dict[str, Any]:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": MODEL,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


def _agent_with_scripted_endpoint(replies: list[Any], seen: list[dict[str, Any]]) -> Any:
    import json

    import httpx
    from agents import Agent, OpenAIChatCompletionsModel, function_tool
    from openai import AsyncOpenAI

    pending = iter(replies)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        body = next(pending)
        if isinstance(body, bytes):
            return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json=body)

    client = AsyncOpenAI(
        api_key="test",
        base_url="https://example.invalid/serving-endpoints",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    # The same swap ``PromptCachedDatabricksOpenAI.chat`` performs.
    cast(Any, client.chat).completions = PromptCachedCompletions(client=client)

    @function_tool
    def lookup() -> str:
        """Look the value up."""
        return "42"

    return Agent(
        name="Supervisor",
        instructions="Answer with the tool.",
        model=OpenAIChatCompletionsModel(model=MODEL, openai_client=client),
        tools=[lookup],
    )


def _tool_turn_completion() -> dict[str, Any]:
    completion = _completion([_reasoning("Need data."), {"type": "text", "text": "Checking."}])
    payload = completion.model_dump(warnings=False)
    payload["choices"][0]["message"]["content"] = [_reasoning("Need data."), {"type": "text", "text": "Checking."}]
    payload["choices"][0]["message"]["tool_calls"] = [_tool_call("toolu_1")]
    payload["choices"][0]["finish_reason"] = "tool_calls"
    return payload


def _answer_completion() -> dict[str, Any]:
    payload = _completion("x").model_dump(warnings=False)
    payload["choices"][0]["message"]["content"] = [
        _reasoning("Got it.", signature="sig-2"),
        {"type": "text", "text": "It is 42."},
    ]
    return payload


class TestAgentRun:
    @pytest.mark.asyncio
    async def test_non_streamed_tool_loop(self) -> None:
        from agents import Runner
        from agents.items import ReasoningItem

        seen: list[dict[str, Any]] = []
        agent = _agent_with_scripted_endpoint([_tool_turn_completion(), _answer_completion()], seen)

        with replay_scope():
            result = await Runner.run(agent, "What is the value?")

        assert result.final_output == "It is 42."
        reasoning = [item for item in result.new_items if isinstance(item, ReasoningItem)]
        assert [item.raw_item.summary[0].text for item in reasoning] == ["Need data.", "Got it."]
        # Step two carried step one's signed reasoning, unmodified.
        assistant = next(m for m in seen[1]["messages"] if m["role"] == "assistant")
        assert assistant["content"][0] == _reasoning("Need data.")
        assert assistant["tool_calls"][0]["id"] == "toolu_1"

    @pytest.mark.asyncio
    async def test_streamed_tool_loop_reaches_invocations_events(self) -> None:
        from agents import Runner

        from payloads.agent.runtime.stream_utils import process_agent_stream_events, start_early_tool_outputs

        tool_turn = _sse(
            [
                _sse_chunk({"role": "assistant", "content": [_reasoning("Need", signature=None)]}),
                _sse_chunk({"content": [_reasoning(" data.", signature="sig-1")]}),
                _sse_chunk({"content": "Checking."}),
                _sse_chunk(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "toolu_1",
                                "type": "function",
                                "function": {"name": "lookup", "arguments": "{}"},
                            }
                        ]
                    }
                ),
                _sse_chunk({}, finish_reason="tool_calls"),
            ]
        )
        answer_turn = _sse(
            [
                _sse_chunk({"role": "assistant", "content": [_reasoning("Got it.", signature="sig-2")]}),
                _sse_chunk({"content": [{"type": "text", "text": "It is 42."}]}),
                _sse_chunk({}, finish_reason="stop"),
            ]
        )
        seen: list[dict[str, Any]] = []
        agent = _agent_with_scripted_endpoint([tool_turn, answer_turn], seen)

        # Only the call is scoped, as in ``agent._run_stream``: the run's
        # task keeps the store after the block exits.  This test is the
        # guard for that: if ``run_streamed`` ever defers its task, the
        # store is gone before the run starts and the ``seen[1]`` assertion
        # below fails.
        hooks, early_tool_outputs = start_early_tool_outputs()
        with replay_scope():
            result = Runner.run_streamed(agent, "What is the value?", hooks=hooks)
        # Raw SDK events arrive as dicts, framework-built ones as models.
        events = [
            e if isinstance(e, dict) else e.model_dump()
            async for e in process_agent_stream_events(
                result.stream_events(),
                early_tool_outputs=early_tool_outputs,
            )
        ]

        reasoning = "".join(e["delta"] for e in events if e.get("type") == "response.reasoning_summary_text.delta")
        assert reasoning == "Need data.Got it."
        assert result.final_output == "It is 42."
        assistant = next(m for m in seen[1]["messages"] if m["role"] == "assistant")
        assert assistant["content"][0] == _reasoning("Need data.", signature="sig-1")
        # The rolling cache breakpoint lands on the text, never on reasoning.
        assert assistant["content"][1]["text"] == "Checking."
        assert "cache_control" in assistant["content"][1]


class TestOmittedSummary:
    """Sonnet 5.5's default without ``display: summarized``: signed, empty.

    Nothing reaches the user -- which is why ``model_settings_for`` asks
    for ``summarized`` -- but the block is still kept for replay.
    """

    def test_non_streamed_empty_summary_builds_no_reasoning_item(self) -> None:
        store = ReasoningReplayStore()
        original = [_reasoning("", signature="sig-1"), {"type": "text", "text": "Checking."}]
        completion = _completion(list(original), tool_calls=[_tool_call("toolu_1")])

        normalize_completion(completion, model=MODEL, store=store)
        items = Converter.message_to_output_items(completion.choices[0].message)

        assert [type(item) for item in items] == [ResponseOutputMessage, ResponseFunctionToolCall]
        entry = store.get(MODEL, "toolu_1")
        assert entry is not None
        assert entry.content == original

    @pytest.mark.asyncio
    async def test_streamed_empty_summary_emits_no_reasoning(self) -> None:
        store = ReasoningReplayStore()
        chunks = [
            _chunk({"role": "assistant", "content": [_reasoning("", signature="sig-1")]}),
            _chunk({"content": "Checking."}),
            _chunk(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "toolu_1",
                            "type": "function",
                            "function": {"name": "lookup", "arguments": "{}"},
                        }
                    ]
                }
            ),
            _chunk({}, finish_reason="tool_calls"),
        ]
        events = await _sdk_events(chunks, store)

        assert not [e for e in events if e.type.startswith("response.reasoning")]
        assert [e.item.type for e in events if e.type == "response.output_item.added"] == ["message", "function_call"]
        assert store.get(MODEL, "toolu_1") is not None


def _agent_module() -> Any:
    """Import the ``@invoke`` / ``@stream`` module without Databricks auth.

    Same pattern as ``test_finalize_hook.py``: ``init_agent_module`` is
    idempotent, so patching it to a no-op is safe even if another test
    imported the module first.
    """
    from unittest.mock import patch

    import payloads.agent.runtime.runtime_config as runtime_config

    with patch.object(runtime_config, "init_agent_module", lambda: None):
        from payloads.agent.runtime import agent
    return agent


class TestInvocations:
    """Reasoning on ``/invocations`` itself: the handlers behind ``@invoke`` / ``@stream``."""

    QUESTION: Any = [{"role": "user", "content": "What is the value?"}]

    @pytest.fixture(autouse=True)
    def _isolated(self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
        import mlflow

        previous = mlflow.get_tracking_uri()
        monkeypatch.delenv("MLFLOW_TRACKING_URI", raising=False)
        mlflow.set_tracking_uri(f"sqlite:///{tmp_path / 'mlflow.db'}")
        yield
        mlflow.set_tracking_uri(previous)

    def _request(self, custom_inputs: dict[str, Any] | None = None) -> Any:
        from mlflow.types.responses import ResponsesAgentRequest

        return ResponsesAgentRequest(input=self.QUESTION, custom_inputs=custom_inputs)

    async def _invoke(self, custom_inputs: dict[str, Any] | None = None) -> dict[str, Any]:
        from payloads.agent.runtime.config import load_agent_config

        config = load_agent_config(None)
        agent = _agent_with_scripted_endpoint([_tool_turn_completion(), _answer_completion()], [])
        response = await _agent_module()._run_invoke(
            agent, list(self.QUESTION), config["MAX_ITERATIONS"], self._request(custom_inputs), config
        )
        return cast(dict[str, Any], response.model_dump())

    async def _stream(self, custom_inputs: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        from payloads.agent.runtime.config import load_agent_config

        config = load_agent_config(None)
        agent = _agent_with_scripted_endpoint([_tool_turn_completion_sse(), _answer_completion_sse()], [])
        events = _agent_module()._run_stream(
            agent, list(self.QUESTION), config["MAX_ITERATIONS"], self._request(custom_inputs), config
        )
        return [e if isinstance(e, dict) else e.model_dump() async for e in events]

    @pytest.mark.asyncio
    async def test_non_streamed_response_carries_reasoning(self) -> None:
        import json

        response = await self._invoke()

        reasoning = [item for item in response["output"] if item["type"] == "reasoning"]
        assert [item["summary"][0]["text"] for item in reasoning] == ["Need data.", "Got it."]
        assert "sig-" not in json.dumps(response, default=str)

    @pytest.mark.asyncio
    async def test_non_streamed_opt_out_drops_reasoning(self) -> None:
        response = await self._invoke({"include_reasoning": False})

        assert all(item["type"] != "reasoning" for item in response["output"])
        texts = [item["content"][0]["text"] for item in response["output"] if item["type"] == "message"]
        assert texts[-1] == "It is 42."

    @pytest.mark.asyncio
    async def test_streamed_response_carries_reasoning(self) -> None:
        import json

        events = await self._stream()

        deltas = "".join(e["delta"] for e in events if e.get("type") == "response.reasoning_summary_text.delta")
        assert deltas == "Need data.Got it."
        done = [
            e for e in events if e.get("type") == "response.output_item.done" and e["item"].get("type") == "reasoning"
        ]
        assert len(done) == 2
        assert "sig-" not in json.dumps(events, default=str)

    @pytest.mark.asyncio
    async def test_streamed_opt_out_drops_reasoning(self) -> None:
        events = await self._stream({"include_reasoning": False})

        assert not [e for e in events if str(e.get("type", "")).startswith("response.reasoning")]
        for event in events:
            item = event.get("item")
            assert not (isinstance(item, dict) and item.get("type") == "reasoning")
            response = event.get("response")
            if isinstance(response, dict):
                assert all(i.get("type") != "reasoning" for i in response.get("output") or [])
        assert any(e.get("type") == "response.output_text.delta" for e in events)

    @pytest.mark.asyncio
    async def test_non_streamed_response_reports_occupancy(self) -> None:
        response = await self._invoke()

        assert response["custom_outputs"]["context_window"]["estimated_input_tokens"] > 0

    @pytest.mark.asyncio
    async def test_streamed_response_reports_occupancy(self) -> None:
        events = await self._stream()

        completed = [e for e in events if e.get("type") == "response.completed" and e.get("custom_outputs")]
        assert len(completed) == 1
        assert completed[0]["custom_outputs"]["context_window"]["estimated_input_tokens"] > 0

    @pytest.mark.asyncio
    async def test_non_streamed_opt_in_exports_the_signed_content(self) -> None:
        response = await self._invoke({REASONING_REPLAY_KEY: []})

        exported = response["custom_outputs"][REASONING_REPLAY_KEY]
        assert exported == [
            {
                "model": MODEL,
                "tool_call_ids": ["toolu_1"],
                "content": [_reasoning("Need data."), {"type": "text", "text": "Checking."}],
            }
        ]

    @pytest.mark.asyncio
    async def test_streamed_opt_in_exports_the_signed_content(self) -> None:
        events = await self._stream({REASONING_REPLAY_KEY: []})

        completed = [e for e in events if e.get("type") == "response.completed" and e.get("custom_outputs")]
        exported = completed[0]["custom_outputs"][REASONING_REPLAY_KEY]
        assert [entry["tool_call_ids"] for entry in exported] == [["toolu_1"]]
        assert exported[0]["content"][0] == _reasoning("Need data.")

    @pytest.mark.asyncio
    async def test_reasoning_opt_out_also_stops_the_export(self) -> None:
        import json

        response = await self._invoke({REASONING_REPLAY_KEY: [], "include_reasoning": False})

        assert REASONING_REPLAY_KEY not in (response["custom_outputs"] or {})
        assert "sig-" not in json.dumps(response, default=str)

    @pytest.mark.asyncio
    async def test_seeded_entries_put_earlier_turns_reasoning_back_on_the_wire(self) -> None:
        from mlflow.types.responses import ResponsesAgentRequest

        from payloads.agent.runtime.config import load_agent_config

        history: Any = [
            {"role": "user", "content": "What was it before?"},
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Checking."}]},
            {"type": "function_call", "call_id": "toolu_0", "name": "lookup", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "toolu_0", "output": "41"},
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "It was 41."}]},
            *self.QUESTION,
        ]
        seed = [_entry("toolu_0").model_dump()]
        request = ResponsesAgentRequest(input=history, custom_inputs={REASONING_REPLAY_KEY: seed})
        config = load_agent_config(None)
        seen: list[dict[str, Any]] = []
        agent = _agent_with_scripted_endpoint([_tool_turn_completion(), _answer_completion()], seen)

        response = await _agent_module()._run_invoke(agent, history, config["MAX_ITERATIONS"], request, config)

        earlier = next(m for m in seen[0]["messages"] if m.get("tool_calls") and m["tool_calls"][0]["id"] == "toolu_0")
        assert earlier["content"][0] == _reasoning("Need data.")
        exported = response.model_dump()["custom_outputs"][REASONING_REPLAY_KEY]
        assert [entry["tool_call_ids"] for entry in exported] == [["toolu_1"]]

    @pytest.mark.asyncio
    async def test_earlier_parallel_calls_go_back_as_one_assistant_message(self) -> None:
        from mlflow.types.responses import ResponsesAgentRequest

        from payloads.agent.runtime.config import load_agent_config

        history: Any = [
            {"role": "user", "content": "What was it before?"},
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Checking."}]},
            _call("toolu_a"),
            _output("toolu_a"),
            _call("toolu_b"),
            _output("toolu_b"),
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "It was 41."}]},
            *self.QUESTION,
        ]
        seed = [_group("toolu_a", "toolu_b").model_dump()]
        request = ResponsesAgentRequest(input=history, custom_inputs={REASONING_REPLAY_KEY: seed})
        config = load_agent_config(None)
        seen: list[dict[str, Any]] = []
        agent = _agent_with_scripted_endpoint([_tool_turn_completion(), _answer_completion()], seen)

        await _agent_module()._run_invoke(agent, history, config["MAX_ITERATIONS"], request, config)

        earlier = [m for m in seen[0]["messages"] if m["role"] == "assistant" and m.get("tool_calls")]
        assert [[call["id"] for call in m["tool_calls"]] for m in earlier] == [["toolu_a", "toolu_b"]]
        assert earlier[0]["content"][0] == _reasoning("Need data.")

    @pytest.mark.asyncio
    async def test_a_group_only_entry_regroups_calls_made_without_reasoning(self) -> None:
        from mlflow.types.responses import ResponsesAgentRequest

        from payloads.agent.runtime.config import load_agent_config

        history: Any = [
            {"role": "user", "content": "What was it before?"},
            _call("toolu_a"),
            _output("toolu_a"),
            _call("toolu_b"),
            _output("toolu_b"),
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "It was 41."}]},
            *self.QUESTION,
        ]
        group = {"model": MODEL, "tool_call_ids": ["toolu_a", "toolu_b"], "content": []}
        request = ResponsesAgentRequest(input=history, custom_inputs={REASONING_REPLAY_KEY: [group]})
        config = load_agent_config(None)
        seen: list[dict[str, Any]] = []
        agent = _agent_with_scripted_endpoint([_tool_turn_completion(), _answer_completion()], seen)

        await _agent_module()._run_invoke(agent, history, config["MAX_ITERATIONS"], request, config)

        earlier = [m for m in seen[0]["messages"] if m["role"] == "assistant" and m.get("tool_calls")]
        assert [[call["id"] for call in m["tool_calls"]] for m in earlier] == [["toolu_a", "toolu_b"]]
        assert not isinstance(earlier[0]["content"], list)


def _tool_turn_completion_sse() -> bytes:
    return _sse(
        [
            _sse_chunk({"role": "assistant", "content": [_reasoning("Need data.")]}),
            _sse_chunk({"content": [{"type": "text", "text": "Checking."}]}),
            _sse_chunk(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "toolu_1",
                            "type": "function",
                            "function": {"name": "lookup", "arguments": "{}"},
                        }
                    ]
                }
            ),
            _sse_chunk({}, finish_reason="tool_calls"),
        ]
    )


def _answer_completion_sse() -> bytes:
    return _sse(
        [
            _sse_chunk({"role": "assistant", "content": [_reasoning("Got it.", signature="sig-2")]}),
            _sse_chunk({"content": [{"type": "text", "text": "It is 42."}]}),
            _sse_chunk({}, finish_reason="stop"),
        ]
    )
