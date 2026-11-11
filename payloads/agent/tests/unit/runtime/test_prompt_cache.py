"""Tests for payloads/agent/runtime/prompt_cache.py -- cache breakpoints."""

from __future__ import annotations

import copy
from typing import Any

import httpx
import pytest

from payloads.agent.config import AgentConfig
from payloads.agent.runtime.prompt_cache import (
    CONTEXT_HEADER,
    CONTEXT_ROLE,
    CONTEXT_SENTINEL,
    MAX_BREAKPOINTS,
    USER_TAIL_ROLE,
    USER_TAIL_SENTINEL,
    PromptCachedCompletions,
    PromptCacheSettings,
    apply_cache_breakpoints,
    configure_prompt_cache,
    get_prompt_cache_settings,
    is_cacheable_model,
    move_context_to_end,
    split_user_tail_message,
)

CLAUDE = "databricks-claude-sonnet-4-5"

# Explicit settings for the pure-function tests so they never depend on
# whatever the process happens to have configured.
SETTINGS = PromptCacheSettings(enabled=True, static_ttl="1h", rolling_ttl=None)


def _markers(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every ``cache_control`` marker in *messages*, in order."""
    found: list[dict[str, Any]] = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, list):
            found.extend(
                block["cache_control"] for block in content if isinstance(block, dict) and "cache_control" in block
            )
    return found


def _recording_client(seen: dict[str, Any]) -> Any:
    """An ``AsyncOpenAI`` whose transport records the serialised request body.

    Real client, mocked transport: what reaches ``seen`` is what would reach
    the endpoint, which is the point of the tests that use it.
    """
    import json

    from openai import AsyncOpenAI

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "1",
                "object": "chat.completion",
                "created": 0,
                "model": CLAUDE,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            },
        )

    return AsyncOpenAI(
        api_key="test",
        base_url="https://example.invalid/serving-endpoints",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def _conversation() -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "First question"},
        {"role": "assistant", "content": "First answer"},
        {"role": "user", "content": "Second question"},
    ]


class TestIsCacheableModel:
    """Capability detection, and parity with the upstream private check."""

    @pytest.mark.parametrize(
        "model",
        [CLAUDE, "claude-3-7-sonnet", "CLAUDE-OPUS", "databricks-claude-opus-4-1"],
    )
    def test_claude_variants_are_cacheable(self, model: str) -> None:
        assert is_cacheable_model(model) is True

    @pytest.mark.parametrize("model", [None, "", "gpt-4o", "databricks-llama-4-maverick"])
    def test_others_are_not(self, model: str | None) -> None:
        assert is_cacheable_model(model) is False

    def test_parity_with_upstream_private_check(self) -> None:
        """Pin agreement with ``databricks_openai``'s own model check.

        We own the one-line check rather than importing a private symbol;
        this test is where an upstream change surfaces loudly.
        """
        from databricks_openai.utils.clients import _is_claude_model

        for model in [CLAUDE, "gpt-4o", "claude-3-5-haiku", "", None, "databricks-gemma"]:
            assert is_cacheable_model(model) == _is_claude_model(model), model


class TestApplyCacheBreakpoints:
    """Placement policy, eligibility rules and idempotency."""

    def test_non_claude_model_leaves_payload_byte_identical(self) -> None:
        messages = _conversation()
        original = copy.deepcopy(messages)
        apply_cache_breakpoints(messages, "gpt-4o", SETTINGS)
        assert messages == original

    def test_no_messages_is_a_noop(self) -> None:
        apply_cache_breakpoints(None, CLAUDE, SETTINGS)
        apply_cache_breakpoints([], CLAUDE, SETTINGS)

    def test_system_and_user_get_two_breakpoints(self) -> None:
        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello"},
        ]
        apply_cache_breakpoints(messages, CLAUDE, SETTINGS)
        assert len(_markers(messages)) == 2
        assert "cache_control" in messages[0]["content"][-1]

    def test_long_history_marks_at_most_three_including_the_tail(self) -> None:
        messages = [{"role": "system", "content": "sys"}]
        for i in range(10):
            messages.append({"role": "user", "content": f"q{i}"})
            messages.append({"role": "assistant", "content": f"a{i}"})

        apply_cache_breakpoints(messages, CLAUDE, SETTINGS)

        assert len(_markers(messages)) == 3
        assert "cache_control" in messages[-1]["content"][-1], "the tail must be marked"

    def test_ttl_tiers_differ_between_static_and_rolling(self) -> None:
        messages = _conversation()
        apply_cache_breakpoints(messages, CLAUDE, SETTINGS)

        static_marker = messages[0]["content"][-1]["cache_control"]
        assert static_marker == {"type": "ephemeral", "ttl": "1h"}

        rolling = [m for m in _markers(messages) if m is not static_marker]
        assert rolling, "expected rolling breakpoints"
        for marker in rolling:
            assert marker == {"type": "ephemeral"}, "rolling tier omits ttl"

    def test_static_ttl_is_settings_driven(self) -> None:
        messages = _conversation()
        apply_cache_breakpoints(messages, CLAUDE, PromptCacheSettings(static_ttl="5m", rolling_ttl="1h"))
        assert messages[0]["content"][-1]["cache_control"] == {"type": "ephemeral", "ttl": "5m"}

    def test_idempotent_across_repeated_calls(self) -> None:
        messages = _conversation()
        apply_cache_breakpoints(messages, CLAUDE, SETTINGS)
        after_first = copy.deepcopy(messages)

        apply_cache_breakpoints(messages, CLAUDE, SETTINGS)

        assert messages == after_first

    def test_assistant_tool_call_with_none_content_is_skipped(self) -> None:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "1"}]},
        ]
        apply_cache_breakpoints(messages, CLAUDE, SETTINGS)

        assert messages[2]["content"] is None, "empty content must stay untouched for the library's own fix"

    def test_empty_string_content_is_skipped(self) -> None:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": "sys"},
            {"role": "assistant", "content": ""},
        ]
        apply_cache_breakpoints(messages, CLAUDE, SETTINGS)

        assert messages[1]["content"] == ""

    def test_trailing_tool_result_is_marked(self) -> None:
        """Tool results carry most of the re-sent tokens, so no role is excluded."""
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "1"}]},
            {"role": "tool", "tool_call_id": "1", "content": "a large tool result"},
        ]
        apply_cache_breakpoints(messages, CLAUDE, SETTINGS)

        assert "cache_control" in messages[-1]["content"][-1]

    def test_string_content_becomes_a_text_block_preserving_text(self) -> None:
        messages: list[dict[str, Any]] = [{"role": "system", "content": "exact text"}]
        apply_cache_breakpoints(messages, CLAUDE, SETTINGS)

        block = messages[0]["content"][0]
        assert block["type"] == "text"
        assert block["text"] == "exact text"

    def test_max_breakpoints_one_marks_only_the_static_block(self) -> None:
        messages = _conversation()
        apply_cache_breakpoints(messages, CLAUDE, SETTINGS, max_breakpoints=1)

        assert len(_markers(messages)) == 1
        assert "cache_control" in messages[0]["content"][-1]

    def test_disabled_settings_leave_payload_byte_identical(self) -> None:
        messages = _conversation()
        original = copy.deepcopy(messages)
        apply_cache_breakpoints(messages, CLAUDE, PromptCacheSettings(enabled=False))
        assert messages == original

    def test_static_breakpoint_precedes_rolling_ones(self) -> None:
        """Provider constraint: longer TTLs must appear before shorter ones."""
        messages = _conversation()
        apply_cache_breakpoints(messages, CLAUDE, PromptCacheSettings(static_ttl="1h", rolling_ttl="5m"))

        ttls = [marker.get("ttl") for marker in _markers(messages)]
        assert ttls[0] == "1h"
        assert all(ttl == "5m" for ttl in ttls[1:])

    def test_no_message_carries_two_markers(self) -> None:
        """A system-only payload must not have the rolling pass re-mark it."""
        messages: list[dict[str, Any]] = [{"role": "system", "content": "sys"}]
        apply_cache_breakpoints(messages, CLAUDE, SETTINGS)

        assert len(_markers(messages)) == 1

    def test_preexisting_marker_short_circuits(self) -> None:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": [{"type": "text", "text": "sys", "cache_control": {"type": "ephemeral"}}]},
            {"role": "user", "content": "q"},
        ]
        apply_cache_breakpoints(messages, CLAUDE, SETTINGS)

        assert len(_markers(messages)) == 1
        assert isinstance(messages[1]["content"], str), "second pass must not convert anything"


def _system_with_user_tail(
    body: str = "Prompt body.",
    tail: str = "## Loadable skills",
) -> dict[str, Any]:
    """The system message as ``create_agent`` assembles it."""
    return {"role": "system", "content": f"{body}\n\n{USER_TAIL_SENTINEL}{tail}"}


class TestSplitUserTailMessage:
    """The user tail must land *outside* the cached prefix, in its own message.

    Caching is cumulative from the start of the prompt, so a tail left in
    the system message would be pulled into the cached prefix and make
    every new user a full-price write of the whole prompt.
    """

    def test_user_tail_becomes_a_message_of_its_own(self) -> None:
        messages = [_system_with_user_tail(), {"role": "user", "content": "q"}]

        split_user_tail_message(messages)

        assert [m["role"] for m in messages] == ["system", USER_TAIL_ROLE, "user"]
        assert messages[0]["content"] == "Prompt body."
        assert messages[1]["content"] == "## Loadable skills"

    def test_user_tail_sits_after_the_breakpoint(self) -> None:
        messages = [_system_with_user_tail(), {"role": "user", "content": "q"}]

        split_user_tail_message(messages)
        apply_cache_breakpoints(messages, CLAUDE, SETTINGS)

        marked = [
            i for i, m in enumerate(messages) if isinstance(m["content"], list) and "cache_control" in m["content"][-1]
        ]
        assert 0 in marked, "the static prompt carries the long-TTL marker"
        assert messages[1]["content"] == "## Loadable skills", "the user tail stays a plain, unmarked message"

    def test_a_different_user_leaves_the_cached_message_untouched(self) -> None:
        """The measured claim, asserted directly: the cached bytes hold."""
        first = [_system_with_user_tail(tail="## Loadable skills\n- alice-report")]
        second = [_system_with_user_tail(tail="## Loadable skills\n- bob-report")]

        for messages in (first, second):
            split_user_tail_message(messages)
            apply_cache_breakpoints(messages, CLAUDE, SETTINGS)

        assert first[0] == second[0]
        assert first[1] != second[1]

    def test_the_sentinel_never_survives_the_split(self) -> None:
        messages = [_system_with_user_tail(), {"role": "user", "content": "q"}]

        split_user_tail_message(messages)

        assert USER_TAIL_SENTINEL not in str(messages)

    def test_a_message_without_the_sentinel_is_untouched(self) -> None:
        """No skill index or model note: nothing to lift out."""
        messages: list[dict[str, Any]] = [{"role": "system", "content": "Prompt body."}]

        split_user_tail_message(messages)

        assert messages == [{"role": "system", "content": "Prompt body."}]

    def test_an_empty_user_tail_adds_no_message(self) -> None:
        messages: list[dict[str, Any]] = [{"role": "system", "content": f"Prompt body.\n\n{USER_TAIL_SENTINEL}"}]

        split_user_tail_message(messages)

        assert messages == [{"role": "system", "content": "Prompt body."}]

    def test_the_split_runs_even_when_caching_is_off(self) -> None:
        """Otherwise a non-Claude endpoint would be shown the sentinel."""
        messages = [_system_with_user_tail()]

        split_user_tail_message(messages)
        apply_cache_breakpoints(messages, "gpt-4o", SETTINGS)

        assert messages == [
            {"role": "system", "content": "Prompt body."},
            {"role": USER_TAIL_ROLE, "content": "## Loadable skills"},
        ]

    def test_splitting_is_idempotent(self) -> None:
        """``create()`` may be retried with a list that was already split."""
        messages = [_system_with_user_tail(), {"role": "user", "content": "q"}]

        split_user_tail_message(messages)
        once = copy.deepcopy(messages)
        split_user_tail_message(messages)

        assert messages == once

    def test_block_content_is_handled_too(self) -> None:
        """Nothing produces this today; the function should not care."""
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": [{"type": "text", "text": f"Prompt body.\n\n{USER_TAIL_SENTINEL}## Skills"}],
            }
        ]

        split_user_tail_message(messages)

        assert messages[0]["content"] == [{"type": "text", "text": "Prompt body."}]
        assert messages[1] == {"role": USER_TAIL_ROLE, "content": "## Skills"}

    def test_a_block_left_empty_is_dropped(self) -> None:
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": [
                    {"type": "text", "text": "Body."},
                    {"type": "text", "text": f"{USER_TAIL_SENTINEL}## Skills"},
                ],
            }
        ]

        split_user_tail_message(messages)

        assert messages[0]["content"] == [{"type": "text", "text": "Body."}]
        assert messages[1]["content"] == "## Skills"


def _history() -> list[dict[str, Any]]:
    return [
        {"role": "user", "content": "q1"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "run_sql", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "rows"},
    ]


def _system_with_context(context: str = "## Current plan") -> dict[str, Any]:
    """The system message with both tails, as ``create_agent`` assembles it."""
    return {
        "role": "system",
        "content": f"Prompt body.\n\n{USER_TAIL_SENTINEL}## Loadable skills\n\n{CONTEXT_SENTINEL}{context}",
    }


def _framed(context: str = "## Current plan") -> str:
    """The trailing message :func:`move_context_to_end` builds from *context*."""
    return f"{CONTEXT_HEADER}\n\n{context}"


def _prepare(messages: list[dict[str, Any]]) -> None:
    """The rewrite order of :meth:`PromptCachedCompletions.create`."""
    move_context_to_end(messages)
    split_user_tail_message(messages)
    apply_cache_breakpoints(messages, CLAUDE, SETTINGS)


class TestMoveContextToEnd:
    """The per-request context must follow the history, outside every cached prefix."""

    def test_context_becomes_the_last_message(self) -> None:
        messages = [_system_with_context(), *_history()]

        move_context_to_end(messages)
        split_user_tail_message(messages)

        assert [m["role"] for m in messages] == ["system", USER_TAIL_ROLE, "user", "assistant", "tool", CONTEXT_ROLE]
        assert messages[0]["content"] == "Prompt body."
        assert messages[1]["content"] == "## Loadable skills"
        assert messages[-1]["content"] == _framed()
        assert CONTEXT_SENTINEL not in str(messages)

    def test_rolling_breakpoints_end_on_the_history(self) -> None:
        messages = [_system_with_context(), *_history()]

        _prepare(messages)

        assert messages[-1]["content"] == _framed(), "the context stays a plain, unmarked message"
        assert messages[-2]["content"][-1]["cache_control"] == {"type": "ephemeral"}
        assert len(_markers(messages)) <= MAX_BREAKPOINTS

    def test_a_different_context_leaves_every_cached_message_untouched(self) -> None:
        first = [_system_with_context("## Current plan\n- [>] a"), *_history()]
        second = [_system_with_context("## Current plan\n- [x] a"), *_history()]

        for messages in (first, second):
            _prepare(messages)

        assert first[:-1] == second[:-1]
        assert first[-1] != second[-1]

    def test_context_without_an_user_tail(self) -> None:
        messages: list[dict[str, Any]] = [{"role": "system", "content": f"Body.\n\n{CONTEXT_SENTINEL}[Context: Mon]"}]

        move_context_to_end(messages)
        split_user_tail_message(messages)

        assert messages == [
            {"role": "system", "content": "Body."},
            {"role": CONTEXT_ROLE, "content": _framed("[Context: Mon]")},
        ]

    def test_an_empty_context_adds_no_message(self) -> None:
        messages: list[dict[str, Any]] = [{"role": "system", "content": f"Body.\n\n{CONTEXT_SENTINEL}"}]

        move_context_to_end(messages)

        assert messages == [{"role": "system", "content": "Body."}]

    def test_moving_is_idempotent(self) -> None:
        messages = [_system_with_context(), *_history()]

        move_context_to_end(messages)
        once = copy.deepcopy(messages)
        move_context_to_end(messages)

        assert messages == once

    @pytest.mark.asyncio
    async def test_create_sends_the_context_last(self) -> None:
        seen: list[dict[str, Any]] = []
        completions = PromptCachedCompletions(client=_scripted_client([_thinking_completion()], seen))

        await completions.create(model=CLAUDE, messages=[_system_with_context(), *_history()])

        sent = seen[0]["messages"]
        assert [m["role"] for m in sent][-1] == CONTEXT_ROLE
        assert sent[-1]["content"] == _framed()
        assert CONTEXT_SENTINEL not in str(seen)


class TestPromptCacheSettings:
    """Config bridge, including agreement with the authored defaults."""

    def test_from_empty_config_matches_agent_config_defaults(self) -> None:
        defaults = AgentConfig()
        settings = PromptCacheSettings.from_config({})

        assert settings.enabled == defaults.prompt_cache_enabled
        assert settings.static_ttl == defaults.prompt_cache_static_ttl
        assert settings.rolling_ttl == defaults.prompt_cache_rolling_ttl

    def test_reads_authored_values(self) -> None:
        settings = PromptCacheSettings.from_config(
            {
                "PROMPT_CACHE_ENABLED": False,
                "PROMPT_CACHE_STATIC_TTL": "5m",
                "PROMPT_CACHE_ROLLING_TTL": "1h",
            }
        )
        assert settings == PromptCacheSettings(enabled=False, static_ttl="5m", rolling_ttl="1h")

    def test_configure_installs_process_settings(self) -> None:
        original = get_prompt_cache_settings()
        try:
            configure_prompt_cache({"PROMPT_CACHE_ENABLED": False, "PROMPT_CACHE_STATIC_TTL": "5m"})
            assert get_prompt_cache_settings() == PromptCacheSettings(enabled=False, static_ttl="5m", rolling_ttl=None)
        finally:
            configure_prompt_cache(
                {
                    "PROMPT_CACHE_ENABLED": original.enabled,
                    "PROMPT_CACHE_STATIC_TTL": original.static_ttl,
                    "PROMPT_CACHE_ROLLING_TTL": original.rolling_ttl,
                }
            )

    def test_apply_falls_back_to_process_settings(self) -> None:
        original = get_prompt_cache_settings()
        try:
            configure_prompt_cache({"PROMPT_CACHE_ENABLED": False})
            messages = _conversation()
            expected = copy.deepcopy(messages)
            apply_cache_breakpoints(messages, CLAUDE)
            assert messages == expected
        finally:
            configure_prompt_cache(
                {
                    "PROMPT_CACHE_ENABLED": original.enabled,
                    "PROMPT_CACHE_STATIC_TTL": original.static_ttl,
                    "PROMPT_CACHE_ROLLING_TTL": original.rolling_ttl,
                }
            )


class TestClientWiring:
    """The seam that carries breakpoints to the wire."""

    def test_chat_completions_is_the_caching_subclass(self) -> None:
        """A ``databricks-openai`` upgrade that reshapes ``chat`` fails here.

        Without this, such a change would silently disable caching in
        production instead of breaking a test.
        """
        from unittest.mock import MagicMock, patch

        from payloads.agent.runtime.prompt_cache import PromptCachedDatabricksOpenAI

        with patch("databricks_openai.utils.clients.WorkspaceClient", MagicMock()):
            client = PromptCachedDatabricksOpenAI()

        assert isinstance(client.chat.completions, PromptCachedCompletions)

    @pytest.mark.parametrize(
        ("llm_kind", "path"),
        [("model_service", "/ai-gateway/mlflow/v1/"), ("serving_endpoint", "/serving-endpoints/")],
    )
    def test_llm_kind_selects_base_url_on_one_client_class(self, llm_kind: str, path: str) -> None:
        from unittest.mock import MagicMock, patch

        from payloads.agent.runtime.prompt_cache import PromptCachedDatabricksOpenAI, build_llm_client

        workspace = MagicMock()
        workspace.config.host = "https://example.cloud.databricks.com"
        with patch("databricks_openai.utils.clients.WorkspaceClient", return_value=workspace):
            client = build_llm_client(llm_kind)

        assert type(client) is PromptCachedDatabricksOpenAI
        assert str(client.base_url) == f"https://example.cloud.databricks.com{path}"
        assert isinstance(client.chat.completions, PromptCachedCompletions)

    def test_model_service_swaps_default_client_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from payloads.agent.runtime import runtime_config

        built: list[str] = []
        installed: list[object] = []
        monkeypatch.setattr(runtime_config, "_gateway_client_installed", False)
        monkeypatch.setattr("payloads.agent.runtime.prompt_cache.build_llm_client", built.append)
        monkeypatch.setattr("agents.set_default_openai_client", installed.append)
        monkeypatch.setattr("payloads.agent.runtime.tracing.configure_openai_autolog", lambda client: None)

        runtime_config.configure_llm_client({"LLM_KIND": "serving_endpoint"})
        assert built == []

        config = {"LLM_KIND": "model_service", "LLM_ENDPOINT_NAME": "system.ai.claude-sonnet-4-5"}
        runtime_config.configure_llm_client(config)
        runtime_config.configure_llm_client(config)
        assert built == ["model_service"]
        assert len(installed) == 1

    def test_model_service_fqn_keeps_prompt_caching(self) -> None:
        assert is_cacheable_model("system.ai.claude-sonnet-4-5") is True

    @pytest.mark.asyncio
    async def test_openai_client_preserves_cache_control_on_the_wire(self) -> None:
        """Pin that the ``openai`` serializer does not strip our marker.

        The whole scheme depends on ``cache_control`` surviving request
        serialization, which is a property of a third-party library rather
        than of our code -- so it gets a test.
        """
        seen: dict[str, Any] = {}
        messages = _conversation()
        apply_cache_breakpoints(messages, CLAUDE, SETTINGS)

        await _recording_client(seen).chat.completions.create(model=CLAUDE, messages=messages)

        sent_system = seen["messages"][0]["content"]
        assert sent_system[-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}

    @pytest.mark.asyncio
    async def test_user_tail_reaches_the_wire_as_its_own_message(self) -> None:
        """End to end, on the real serializer: two messages, no sentinel.

        The shapes were checked against ``databricks-claude-sonnet-4-6``
        directly: a second ``system`` message is accepted, reads the same
        cached prefix, and re-writes nothing when the tail changes.
        """
        seen: dict[str, Any] = {}
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": f"Prompt body.\n\n{USER_TAIL_SENTINEL}## Loadable skills"},
            {"role": "user", "content": "q"},
        ]
        split_user_tail_message(messages)
        apply_cache_breakpoints(messages, CLAUDE, SETTINGS)

        await _recording_client(seen).chat.completions.create(model=CLAUDE, messages=messages)

        sent = seen["messages"]
        assert [m["role"] for m in sent] == ["system", USER_TAIL_ROLE, "user"]
        assert sent[0]["content"][-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
        assert sent[1]["content"] == "## Loadable skills"
        assert USER_TAIL_SENTINEL not in str(seen)


THINKING = "databricks-claude-sonnet-5-5"

_SIGNED_CONTENT: list[dict[str, Any]] = [
    {"type": "reasoning", "summary": [{"type": "summary_text", "text": "Need data.", "signature": "sig-1"}]},
    {"type": "text", "text": "Checking."},
]


def _thinking_completion() -> dict[str, Any]:
    return {
        "id": "1",
        "object": "chat.completion",
        "created": 0,
        "model": THINKING,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": _SIGNED_CONTENT,
                    "tool_calls": [
                        {"id": "toolu_1", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
    }


def _scripted_client(bodies: list[Any], seen: list[dict[str, Any]]) -> Any:
    """An ``AsyncOpenAI`` that answers each request with the next of *bodies*.

    A ``bytes`` body is served as an SSE stream.  Every request body is
    recorded in *seen*, in order.
    """
    import json

    from openai import AsyncOpenAI

    replies = iter(bodies)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        body = next(replies)
        if isinstance(body, bytes):
            return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json=body)

    return AsyncOpenAI(
        api_key="test",
        base_url="https://example.invalid/serving-endpoints",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def _final_answer() -> dict[str, Any]:
    return {
        "id": "2",
        "object": "chat.completion",
        "created": 0,
        "model": THINKING,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "Done."}, "finish_reason": "stop"}],
    }


def _tool_call_turn(content: Any) -> list[dict[str, Any]]:
    """What the SDK sends after a tool call: the turn without its reasoning."""
    return [
        {"role": "user", "content": "q"},
        {
            "role": "assistant",
            "content": content,
            "tool_calls": [{"id": "toolu_1", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "toolu_1", "content": "42"},
    ]


class TestReasoningWiring:
    """Thinking-model responses through the caching client (#473)."""

    @pytest.mark.asyncio
    async def test_list_content_is_normalized(self) -> None:
        seen: list[dict[str, Any]] = []
        completions = PromptCachedCompletions(client=_scripted_client([_thinking_completion()], seen))

        response = await completions.create(model=THINKING, messages=[{"role": "user", "content": "q"}])

        message = response.choices[0].message
        assert message.content == "Checking."
        assert message.reasoning_content == "Need data."

    @pytest.mark.asyncio
    async def test_streamed_list_content_is_normalized(self) -> None:
        import json

        chunk = {
            "id": "1",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": THINKING,
            "choices": [
                {"index": 0, "delta": {"role": "assistant", "content": _SIGNED_CONTENT}, "finish_reason": None}
            ],
        }
        sse = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode()
        seen: list[dict[str, Any]] = []
        completions = PromptCachedCompletions(client=_scripted_client([sse], seen))

        stream = await completions.create(model=THINKING, messages=[{"role": "user", "content": "q"}], stream=True)
        deltas = [chunk.choices[0].delta async for chunk in stream]

        assert [d.content for d in deltas] == ["Checking."]
        assert [d.reasoning_content for d in deltas] == ["Need data."]

    @pytest.mark.asyncio
    async def test_signed_reasoning_reaches_the_wire_on_the_next_call(self) -> None:
        """The SDK drops reasoning on replay; the client puts it back."""
        from payloads.agent.runtime.reasoning import replay_scope

        seen: list[dict[str, Any]] = []
        completions = PromptCachedCompletions(client=_scripted_client([_thinking_completion(), _final_answer()], seen))

        with replay_scope():
            await completions.create(model=THINKING, messages=[{"role": "user", "content": "q"}])
            await completions.create(model=THINKING, messages=_tool_call_turn("Checking."))

        sent_assistant = seen[1]["messages"][1]
        assert sent_assistant["content"][0] == _SIGNED_CONTENT[0]
        # The marker, if any, sits on the text block -- never on reasoning.
        assert "cache_control" not in sent_assistant["content"][0]
        assert sent_assistant["content"][1]["text"] == "Checking."

    @pytest.mark.asyncio
    async def test_whitespace_before_a_tool_call_never_reaches_the_wire(self) -> None:
        """A blank text block next to reasoning would draw a cache marker and a 400."""
        from payloads.agent.runtime.reasoning import replay_scope

        seen: list[dict[str, Any]] = []
        tool_turn = _thinking_completion()
        tool_turn["choices"][0]["message"]["content"] = [_SIGNED_CONTENT[0], {"type": "text", "text": "\n\n"}]
        completions = PromptCachedCompletions(client=_scripted_client([tool_turn, _final_answer()], seen))

        with replay_scope():
            await completions.create(model=THINKING, messages=[{"role": "user", "content": "q"}])
            await completions.create(model=THINKING, messages=_tool_call_turn("\n\n"))

        assert seen[1]["messages"][1]["content"] == [_SIGNED_CONTENT[0]]

    @pytest.mark.asyncio
    async def test_no_replay_outside_an_agent_run(self) -> None:
        seen: list[dict[str, Any]] = []
        completions = PromptCachedCompletions(client=_scripted_client([_thinking_completion(), _final_answer()], seen))

        await completions.create(model=THINKING, messages=[{"role": "user", "content": "q"}])
        await completions.create(model=THINKING, messages=_tool_call_turn("Checking."))

        # Only the text went out (as a cache-marked block): no reasoning.
        assert [block["type"] for block in seen[1]["messages"][1]["content"]] == ["text"]

    def test_reasoning_only_message_is_never_marked(self) -> None:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "q"},
            {
                "role": "assistant",
                "content": [_SIGNED_CONTENT[0]],
                "tool_calls": [{"id": "toolu_1", "type": "function", "function": {"name": "f", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "toolu_1", "content": "42"},
        ]
        apply_cache_breakpoints(messages, THINKING, SETTINGS)
        assert "cache_control" not in messages[2]["content"][0]
        assert len(_markers(messages)) == 3
