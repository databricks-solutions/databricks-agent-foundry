"""MLflow openai autolog vs Databricks Claude usage shapes (#444)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import mlflow
import pytest
from mlflow.tracing.constant import SpanAttributeKey, TokenUsageKey
from openai import AsyncOpenAI

from payloads.agent.runtime.prompt_cache import PromptCachedCompletions

CLAUDE = "databricks-claude-sonnet-4-5"

DATABRICKS_USAGE: dict[str, Any] = {
    "prompt_tokens": 100,
    "completion_tokens": 20,
    "total_tokens": 120,
    "cache_read_input_tokens": 9203,
    "cache_creation_input_tokens": 10,
    "cache_creation": {
        "ephemeral_1h_input_tokens": 8,
        "ephemeral_5m_input_tokens": 2,
    },
}

EXPECTED_CACHE = {
    TokenUsageKey.INPUT_TOKENS: 100,
    TokenUsageKey.OUTPUT_TOKENS: 20,
    TokenUsageKey.TOTAL_TOKENS: 120,
    TokenUsageKey.CACHE_READ_INPUT_TOKENS: 9203,
    TokenUsageKey.CACHE_CREATION_INPUT_TOKENS: 10,
}


def _completion_json(*, usage: dict[str, Any] | None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 0,
        "model": CLAUDE,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }
        ],
    }
    if usage is not None:
        body["usage"] = usage
    return body


def _chunk(*, usage: dict[str, Any] | None = None, content: str | None = "ok") -> dict[str, Any]:
    chunk: dict[str, Any] = {
        "id": "chatcmpl-stream-1",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": CLAUDE,
        "choices": [],
    }
    if content is not None:
        chunk["choices"] = [
            {
                "index": 0,
                "delta": {"role": "assistant", "content": content},
                "finish_reason": None,
            }
        ]
    if usage is not None:
        chunk["usage"] = usage
        chunk["choices"] = []
    return chunk


def _sse(chunks: list[dict[str, Any]]) -> bytes:
    parts = [f"data: {json.dumps(chunk)}\n\n" for chunk in chunks]
    parts.append("data: [DONE]\n\n")
    return "".join(parts).encode()


def _client(*, json_body: dict[str, Any] | None = None, sse: bytes | None = None) -> AsyncOpenAI:
    def handler(request: httpx.Request) -> httpx.Response:
        if sse is not None:
            return httpx.Response(200, content=sse)
        assert json_body is not None
        return httpx.Response(200, json=json_body)

    return AsyncOpenAI(
        api_key="test",
        base_url="https://example.invalid/serving-endpoints",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def _last_chat_span() -> Any:
    trace_id = mlflow.get_last_active_trace_id()
    assert trace_id is not None
    trace = mlflow.get_trace(trace_id, flush=True)
    assert trace is not None
    return next(span for span in trace.data.spans if span.get_attribute(SpanAttributeKey.CHAT_USAGE) is not None)


@pytest.fixture
def autolog_openai(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.delenv("MLFLOW_TRACKING_URI", raising=False)
    mlflow.set_tracking_uri(f"sqlite:///{tmp_path / 'mlflow.db'}")
    mlflow.openai.autolog()
    yield
    mlflow.openai.autolog(disable=True)


@pytest.mark.asyncio
async def test_nonstream_databricks_usage_reaches_token_usage(autolog_openai: None) -> None:
    client = _client(json_body=_completion_json(usage=DATABRICKS_USAGE))
    completions = PromptCachedCompletions(client=client)
    await completions.create(model=CLAUDE, messages=[{"role": "user", "content": "hi"}])

    span = _last_chat_span()
    usage = span.get_attribute(SpanAttributeKey.CHAT_USAGE)
    assert usage == EXPECTED_CACHE

    outputs = span.outputs
    dumped = outputs.model_dump() if hasattr(outputs, "model_dump") else outputs
    usage_dump = dumped["usage"] if isinstance(dumped, dict) else dumped
    if isinstance(usage_dump, dict):
        extras_present = usage_dump.get("cache_read_input_tokens") == 9203
        nested = usage_dump.get("cache_creation") or {}
        has_1h = (nested.get("ephemeral_1h_input_tokens") if isinstance(nested, dict) else None) == 8
    else:
        extras_present = getattr(usage_dump, "cache_read_input_tokens", None) == 9203
        nested = getattr(usage_dump, "cache_creation", None)
        has_1h = nested is not None and getattr(nested, "ephemeral_1h_input_tokens", None) == 8
    # Pin what the dashboard can read from spanOutputs vs tokenUsage.
    assert extras_present is True
    assert has_1h is True


@pytest.mark.asyncio
async def test_stream_without_usage_chunk_has_no_cache_reads(autolog_openai: None) -> None:
    sse = _sse([_chunk(content="ok")])
    client = _client(sse=sse)
    completions = PromptCachedCompletions(client=client)
    stream = await completions.create(
        model=CLAUDE,
        messages=[{"role": "user", "content": "hi"}],
        stream=True,
    )
    async for _ in stream:
        pass

    trace_id = mlflow.get_last_active_trace_id()
    assert trace_id is not None
    trace = mlflow.get_trace(trace_id, flush=True)
    assert trace is not None
    chat_spans = [span for span in trace.data.spans if span.get_attribute(SpanAttributeKey.CHAT_USAGE)]
    if not chat_spans:
        return
    usage = chat_spans[0].get_attribute(SpanAttributeKey.CHAT_USAGE)
    assert TokenUsageKey.CACHE_READ_INPUT_TOKENS not in (usage or {})


@pytest.mark.asyncio
async def test_stream_with_include_usage_records_cache_reads(autolog_openai: None) -> None:
    sse = _sse([_chunk(content="ok"), _chunk(usage=DATABRICKS_USAGE, content=None)])
    client = _client(sse=sse)
    completions = PromptCachedCompletions(client=client)
    stream = await completions.create(
        model=CLAUDE,
        messages=[{"role": "user", "content": "hi"}],
        stream=True,
        stream_options={"include_usage": True},
    )
    async for _ in stream:
        pass

    span = _last_chat_span()
    usage = span.get_attribute(SpanAttributeKey.CHAT_USAGE)
    assert usage == EXPECTED_CACHE


@pytest.mark.asyncio
# Autolog serializes the raw chunk; Databricks' list-shaped reasoning content
# does not match the OpenAI SDK's str-typed delta.content.
@pytest.mark.filterwarnings("ignore:Pydantic serializer warnings:UserWarning")
async def test_stream_with_reasoning_blocks_is_normalized_under_autolog(autolog_openai: None) -> None:
    """Autolog wraps the raw stream; our wrapper sits outside it (#473)."""
    reasoning_chunk = _chunk(content="placeholder")
    reasoning_chunk["choices"][0]["delta"]["content"] = [
        {"type": "reasoning", "summary": [{"type": "summary_text", "text": "Hmm.", "signature": "s"}]},
        {"type": "text", "text": "ok"},
    ]
    sse = _sse([reasoning_chunk, _chunk(usage=DATABRICKS_USAGE, content=None)])
    completions = PromptCachedCompletions(client=_client(sse=sse))
    stream = await completions.create(
        model=CLAUDE,
        messages=[{"role": "user", "content": "hi"}],
        stream=True,
        stream_options={"include_usage": True},
    )
    texts = [chunk.choices[0].delta.content async for chunk in stream if chunk.choices]

    assert texts == ["ok"]
    assert _last_chat_span().get_attribute(SpanAttributeKey.CHAT_USAGE) == EXPECTED_CACHE
