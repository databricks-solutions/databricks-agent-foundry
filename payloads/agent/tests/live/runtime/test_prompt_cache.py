"""Live Claude stream canary for prompt-cache telemetry (#444).

Calls Model Serving from this process (not the deployed App). Set
``FOUNDRY_PROMPT_CACHE_E2E_ENDPOINT`` (preferred; sonnet 4.5 in CI) or
``FOUNDRY_REASONING_E2E_ENDPOINT``.

A call right after the cache write can still miss (seen on Sonnet 4.5 and
5.5, endpoint and gateway), so the follow-up call is retried before failing.
"""

from __future__ import annotations

import asyncio
import os

import pytest
from openai import AsyncOpenAI

from payloads.agent.runtime.prompt_cache import PromptCachedCompletions

_ENDPOINT_OVERRIDE = "FOUNDRY_PROMPT_CACHE_E2E_ENDPOINT"
_REASONING_OVERRIDE = "FOUNDRY_REASONING_E2E_ENDPOINT"
_PREFIX = ("Stable cache prefix. " * 400).strip()  # well over the ~1024-token minimum
_FOLLOW_UP_ATTEMPTS = 3
_FOLLOW_UP_DELAY_SECONDS = 2.0


@pytest.fixture(scope="module")
def model() -> str:
    name = os.environ.get(_ENDPOINT_OVERRIDE, "").strip() or os.environ.get(_REASONING_OVERRIDE, "").strip()
    if not name:
        pytest.fail(f"set {_ENDPOINT_OVERRIDE} or {_REASONING_OVERRIDE}; these tests call serving from CI, not the App")
    return name


async def _stream_once(completions: PromptCachedCompletions, model: str, user_text: str) -> dict[str, object]:
    messages = [
        {"role": "system", "content": _PREFIX},
        {"role": "user", "content": user_text},
    ]
    stream = await completions.create(
        model=model,
        messages=messages,
        stream=True,
        stream_options={"include_usage": True},
        max_tokens=8,
    )
    last_usage: dict[str, object] = {}
    async for chunk in stream:
        usage = getattr(chunk, "usage", None)
        if usage is None:
            continue
        dumped = usage.model_dump() if hasattr(usage, "model_dump") else dict(usage)
        last_usage = dumped
    assert last_usage, "stream usage chunk missing; include_usage may have been dropped"
    return last_usage


async def _assert_second_call_reads_cache(client: AsyncOpenAI, model: str) -> None:
    if "claude" not in model.lower():
        pytest.skip("live canary is Claude-only (Databricks cache_control)")
    completions = PromptCachedCompletions(client=client)
    usages = [await _stream_once(completions, model, "Say one word.")]
    for _ in range(_FOLLOW_UP_ATTEMPTS):
        await asyncio.sleep(_FOLLOW_UP_DELAY_SECONDS)
        usage = await _stream_once(completions, model, "Say a different word.")
        usages.append(usage)
        cache_read = usage.get("cache_read_input_tokens")
        if isinstance(cache_read, (int, float)) and cache_read > 0:
            return
    pytest.fail(f"no follow-up call read the cache after {_FOLLOW_UP_ATTEMPTS} attempts; usage per call: {usages}")


@pytest.mark.asyncio
async def test_live_streamed_claude_second_call_reads_cache(model: str, serving_client: AsyncOpenAI) -> None:
    await _assert_second_call_reads_cache(serving_client, model)


@pytest.mark.asyncio
async def test_live_model_service_second_call_reads_cache(model_service: str, gateway_client: AsyncOpenAI) -> None:
    await _assert_second_call_reads_cache(gateway_client, model_service)
