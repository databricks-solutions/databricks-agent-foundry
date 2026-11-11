"""Live canary for thinking models on Databricks chat completions (#473).

Calls Model Serving from this process (not the deployed App). Set
``FOUNDRY_REASONING_E2E_ENDPOINT`` (e.g. ``databricks-claude-sonnet-5-5``).

Set ``FOUNDRY_REASONING_E2E_DUMP=<dir>`` to also write the raw response
bodies there -- the reference for the offline fixtures in
``tests/unit/runtime/test_reasoning.py``, whose shapes follow the documented contract.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from openai import AsyncOpenAI, BadRequestError

from payloads.agent.runtime.prompt_cache import PromptCachedCompletions
from payloads.agent.runtime.reasoning import replay_scope

_ENDPOINT_OVERRIDE = "FOUNDRY_REASONING_E2E_ENDPOINT"

_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_temperature",
            "description": "Current temperature in Celsius for one city.",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    }
]

_QUESTION = (
    "Is it warmer in Paris or in Oslo right now, and by how much? "
    "You must call get_temperature once for each city before answering."
)

# ``display: summarized`` is what ``model_settings_for`` sends by default:
# without it Sonnet 5.5 returns the summary empty, and every reasoning
# assertion below would skip.
_THINKING_BODY: dict[str, Any] = {
    "thinking": {"type": "adaptive", "display": "summarized"},
    "reasoning_effort": "high",
}


@pytest.fixture(scope="module")
def model() -> str:
    name = os.environ.get(_ENDPOINT_OVERRIDE, "").strip()
    if not name:
        pytest.fail(f"set {_ENDPOINT_OVERRIDE}; these tests call serving from CI, not the App")
    return name


def _dump(name: str, payload: Any) -> None:
    target = os.environ.get("FOUNDRY_REASONING_E2E_DUMP")
    if not target:
        return
    path = Path(target)
    path.mkdir(parents=True, exist_ok=True)
    (path / name).write_text(json.dumps(payload, indent=2, default=str))


def _tool_results(message: Any) -> list[dict[str, Any]]:
    return [
        {"role": "tool", "tool_call_id": call.id, "content": json.dumps({"celsius": 18 if "aris" in str(call) else 4})}
        for call in message.tool_calls or []
    ]


@pytest.mark.asyncio
async def test_live_raw_shapes_are_recorded(model: str, serving_client: AsyncOpenAI) -> None:
    """Capture the raw non-streamed and streamed bodies for the fixtures."""
    # The plain client, bypassing the normalisation under test: these are
    # the bodies exactly as the endpoint sent them.  ``Any`` because the
    # payloads are plain dicts rather than openai's TypedDict params.
    completions: Any = serving_client.chat.completions
    messages = [{"role": "user", "content": _QUESTION}]

    raw = await completions.with_raw_response.create(
        model=model, messages=messages, tools=_TOOLS, extra_body=_THINKING_BODY
    )
    _dump("nonstream_tool.json", json.loads(raw.text))

    chunks: list[Any] = []
    stream = await completions.create(
        model=model, messages=messages, tools=_TOOLS, stream=True, extra_body=_THINKING_BODY
    )
    async for chunk in stream:
        chunks.append(chunk.model_dump(warnings=False))
    _dump("stream_tool.json", chunks)
    assert chunks


@pytest.mark.asyncio
async def test_live_tool_loop_with_replayed_reasoning(model: str, serving_client: AsyncOpenAI) -> None:
    """Non-streamed tool loop through the client: normalised, replayed, accepted."""
    await _assert_tool_loop_replays_reasoning(serving_client, model)


@pytest.mark.asyncio
async def test_live_model_service_tool_loop_with_replayed_reasoning(
    model_service: str, gateway_client: AsyncOpenAI
) -> None:
    """The same loop through Unity Gateway's unified chat API."""
    await _assert_tool_loop_replays_reasoning(gateway_client, model_service)


async def _assert_tool_loop_replays_reasoning(client: AsyncOpenAI, model: str) -> None:
    completions = PromptCachedCompletions(client=client)
    messages: list[dict[str, Any]] = [{"role": "user", "content": _QUESTION}]

    with replay_scope() as store:
        first = await completions.create(model=model, messages=messages, tools=_TOOLS, extra_body=_THINKING_BODY)
        message = first.choices[0].message
        assert message.content is None or isinstance(message.content, str)
        if not message.tool_calls:
            pytest.skip("model answered without calling the tool")
        if not getattr(message, "reasoning_content", None):
            pytest.skip("model did not think on the tool-call turn")
        assert len(store) == len(message.tool_calls)

        messages.append(
            {
                "role": "assistant",
                "content": message.content,
                "tool_calls": [call.model_dump() for call in message.tool_calls],
            }
        )
        messages.extend(_tool_results(message))
        second = await completions.create(model=model, messages=messages, tools=_TOOLS, extra_body=_THINKING_BODY)

    # The reasoning went back on the wire, and the endpoint took it.
    assert messages[1]["content"][0]["type"] == "reasoning"
    assert second.choices[0].message.content is None or isinstance(second.choices[0].message.content, str)


@pytest.mark.asyncio
async def test_live_streamed_tool_call_is_normalised(model: str, serving_client: AsyncOpenAI) -> None:
    completions = PromptCachedCompletions(client=serving_client)
    reasoning = ""
    with replay_scope() as store:
        stream = await completions.create(
            model=model,
            messages=[{"role": "user", "content": _QUESTION}],
            tools=_TOOLS,
            stream=True,
            extra_body=_THINKING_BODY,
        )
        async for chunk in stream:
            for choice in chunk.choices:
                assert choice.delta.content is None or isinstance(choice.delta.content, str)
                reasoning += getattr(choice.delta, "reasoning_content", None) or ""
    if not reasoning:
        pytest.skip("model did not think on this turn")
    assert len(store) > 0, "signed reasoning behind the tool calls was not stored for replay"


@pytest.mark.asyncio
async def test_live_tool_loop_without_replay_is_reported(model: str, serving_client: AsyncOpenAI) -> None:
    """Informational: does the endpoint insist on replayed reasoning?

    Never fails on the answer -- it records it, because the replay design
    holds either way (Databricks recommends replay for reasoning continuity).
    """
    completions = PromptCachedCompletions(client=serving_client)
    messages: list[dict[str, Any]] = [{"role": "user", "content": _QUESTION}]
    with replay_scope():
        first = await completions.create(model=model, messages=messages, tools=_TOOLS, extra_body=_THINKING_BODY)
    message = first.choices[0].message
    if not message.tool_calls or not getattr(message, "reasoning_content", None):
        pytest.skip("model did not think and call the tool on the first turn")

    # The second call runs outside the scope: the "no replay" shape.
    messages.append(
        {"role": "assistant", "content": message.content, "tool_calls": [c.model_dump() for c in message.tool_calls]}
    )
    messages.extend(_tool_results(message))
    try:
        await completions.create(model=model, messages=messages, tools=_TOOLS, extra_body=_THINKING_BODY)
        outcome = "accepted"
    except BadRequestError as exc:
        outcome = f"rejected: {exc}"
    print(f"[reasoning canary] tool loop without replayed reasoning: {outcome}")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra_body",
    [
        {"thinking": {"type": "adaptive"}},
        {"thinking": {"type": "adaptive", "display": "summarized"}},
        {"thinking": {"type": "adaptive", "display": "omitted"}},
        {"thinking": {"type": "between_tools"}},
        {"reasoning_effort": "low"},
        {"reasoning_effort": "max"},
    ],
    ids=[
        "thinking-adaptive",
        "thinking-adaptive-summarized",
        "thinking-adaptive-omitted",
        "thinking-between_tools",
        "effort-low",
        "effort-max",
    ],
)
async def test_live_request_settings_are_accepted(
    extra_body: dict[str, Any], model: str, serving_client: AsyncOpenAI
) -> None:
    completions = PromptCachedCompletions(client=serving_client)
    response = await completions.create(
        model=model, messages=[{"role": "user", "content": "Say one word."}], extra_body=extra_body
    )
    assert response.choices
