"""Tests for payloads/agent/runtime/tool_result_clearing.py."""

from __future__ import annotations

import json
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from payloads.agent.config import AgentConfig
from payloads.agent.runtime.context_occupancy import OccupancyRecorder
from payloads.agent.runtime.tool_result_clearing import (
    CHARS_PER_TOKEN,
    DEFAULT_CLEAR_ABOVE_INPUT_TOKENS,
    DEFAULT_CLEAR_AT_LEAST_CHARS,
    ToolResultClearingSettings,
    apply_tool_result_clearing,
    build_call_model_input_filter,
    configure_tool_result_clearing,
    estimate_input_tokens,
    get_tool_result_clearing_settings,
    run_config_for,
    tool_schema_chars,
)

# The latest round's result: long enough to be clearable, so it takes a keep slot.
RECENT = "recent " * 30


@dataclass
class _FakeCallModelData:
    """Minimal stand-in for the SDK's ``CallModelData``."""

    model_data: Any
    agent: Any = None
    context: Any = None


def _tool_output(call_id: str, output: str) -> dict[str, Any]:
    return {"type": "function_call_output", "call_id": call_id, "output": output}


def _tool_call(call_id: str, name: str) -> dict[str, Any]:
    return {"type": "function_call", "call_id": call_id, "name": name, "arguments": "{}"}


def _assistant_reply() -> dict[str, Any]:
    """A model turn after the outputs, so they count as already read."""
    return {"type": "message", "role": "assistant", "content": "noted"}


def test_settings_from_config_defaults() -> None:
    settings = ToolResultClearingSettings.from_config({})
    assert settings.enabled is True
    assert settings.keep_tool_uses == 3
    assert settings.clear_at_least_chars == DEFAULT_CLEAR_AT_LEAST_CHARS
    assert settings.clear_above_input_tokens == DEFAULT_CLEAR_ABOVE_INPUT_TOKENS


def test_settings_defaults_match_agent_config() -> None:
    """``AgentConfig`` repeats these literals instead of importing them, because
    it also loads on the deploy path where the Agents SDK is absent.  This test
    is what keeps the two copies from drifting apart.
    """
    settings = ToolResultClearingSettings.from_config({})
    defaults = AgentConfig()
    assert settings.enabled == defaults.tool_result_clearing_enabled
    assert settings.keep_tool_uses == defaults.tool_result_clearing_keep_tool_uses
    assert settings.clear_at_least_chars == defaults.tool_result_clearing_clear_at_least_chars
    assert settings.clear_above_input_tokens == defaults.tool_result_clearing_clear_above_input_tokens


def test_settings_from_config_overrides() -> None:
    settings = ToolResultClearingSettings.from_config(
        {
            "TOOL_RESULT_CLEARING_ENABLED": False,
            "TOOL_RESULT_CLEARING_KEEP_TOOL_USES": 5,
            "TOOL_RESULT_CLEARING_CLEAR_AT_LEAST_CHARS": 10000,
            "TOOL_RESULT_CLEARING_CLEAR_ABOVE_INPUT_TOKENS": 50000,
        }
    )
    assert settings.enabled is False
    assert settings.keep_tool_uses == 5
    assert settings.clear_at_least_chars == 10000
    assert settings.clear_above_input_tokens == 50000


def test_configure_tool_result_clearing() -> None:
    original = get_tool_result_clearing_settings()
    try:
        configure_tool_result_clearing(
            {"TOOL_RESULT_CLEARING_ENABLED": False, "TOOL_RESULT_CLEARING_KEEP_TOOL_USES": 2}
        )
        assert get_tool_result_clearing_settings() == ToolResultClearingSettings(enabled=False, keep_tool_uses=2)
    finally:
        configure_tool_result_clearing(
            {
                "TOOL_RESULT_CLEARING_ENABLED": original.enabled,
                "TOOL_RESULT_CLEARING_KEEP_TOOL_USES": original.keep_tool_uses,
                "TOOL_RESULT_CLEARING_CLEAR_AT_LEAST_CHARS": (original.clear_at_least_chars),
                "TOOL_RESULT_CLEARING_CLEAR_ABOVE_INPUT_TOKENS": (original.clear_above_input_tokens),
            }
        )


def test_apply_keeps_last_three_outputs() -> None:
    items = [
        _tool_call("c1", "search"),
        _tool_output("c1", "result-1" * 100),
        _tool_call("c2", "search"),
        _tool_output("c2", "result-2" * 100),
        _tool_call("c3", "search"),
        _tool_output("c3", "result-3" * 100),
        _tool_call("c4", "search"),
        _tool_output("c4", "result-4" * 100),
    ]
    cleared = apply_tool_result_clearing(items, keep_tool_uses=3, clear_at_least_chars=0)
    assert cleared[1]["output"] == ("[Tool result cleared (search) to conserve context. Re-call the tool if needed.]")
    assert cleared[3]["output"] == "result-2" * 100
    assert cleared[5]["output"] == "result-3" * 100
    assert cleared[7]["output"] == "result-4" * 100


def _parallel_batch(size: int) -> list[dict[str, Any]]:
    calls = [_tool_call(f"c{index}", "search") for index in range(size)]
    outputs = [_tool_output(f"c{index}", f"result-{index}" * 100) for index in range(size)]
    return [*calls, *outputs]


def test_apply_never_clears_an_unread_parallel_batch() -> None:
    items = _parallel_batch(6)
    assert apply_tool_result_clearing(items, keep_tool_uses=3, clear_at_least_chars=0) is items


def test_apply_clears_a_parallel_batch_once_the_model_has_read_it() -> None:
    items = [*_parallel_batch(6), *_parallel_batch(2), _assistant_reply()]
    cleared = apply_tool_result_clearing(items, keep_tool_uses=1, clear_at_least_chars=0)
    outputs = [item["output"] for item in cleared if item.get("type") == "function_call_output"]
    assert [output.startswith("[Tool result cleared") for output in outputs] == [True] * 6 + [False] * 2


def test_keep_counts_a_parallel_batch_as_one_round() -> None:
    items = [*_parallel_batch(6), _assistant_reply()]
    assert apply_tool_result_clearing(items, keep_tool_uses=1, clear_at_least_chars=0) is items


def _named_round(call_id: str, name: str, output: str) -> list[dict[str, Any]]:
    return [_tool_call(call_id, name), _tool_output(call_id, output)]


def _outputs(items: list[Any]) -> dict[str, str]:
    return {item["call_id"]: item["output"] for item in items if item.get("type") == "function_call_output"}


def test_plan_rounds_between_sql_rounds_take_no_keep_slot() -> None:
    """Issue #671: plan updates interleaved with SQL must not push recent SQL out."""
    plan_reply = "Plan saved: 1 of 4 done. Open: 2. Views mapped; 3. Owners listed; 4. Report written" * 2
    items = [
        *_named_round("s1", "sql", "A" * 1000),
        *_named_round("p1", "_update_plan", plan_reply),
        *_named_round("s2", "sql", "B" * 1000),
        *_named_round("p2", "_update_plan", plan_reply),
        *_named_round("s3", "sql", "C" * 1000),
        *_named_round("p3", "_update_plan", plan_reply),
        _assistant_reply(),
    ]
    outputs = _outputs(apply_tool_result_clearing(items, keep_tool_uses=2, clear_at_least_chars=0))
    assert outputs["s1"].startswith("[Tool result cleared (sql)")
    assert (outputs["s2"], outputs["s3"]) == ("B" * 1000, "C" * 1000)
    assert {outputs[call_id] for call_id in ("p1", "p2", "p3")} == {plan_reply}


@pytest.mark.parametrize("name", ["_load_skill", "_recall", "_list_memories"])
def test_exempt_tool_outputs_are_never_cleared(name: str) -> None:
    items = [*_named_round("x1", name, "S" * 5000), *_named_round("s1", "sql", RECENT), _assistant_reply()]
    outputs = _outputs(apply_tool_result_clearing(items, keep_tool_uses=0, clear_at_least_chars=0))
    assert outputs["x1"] == "S" * 5000
    assert outputs["s1"].startswith("[Tool result cleared (sql)")


def test_rounds_with_only_tiny_outputs_take_no_keep_slot() -> None:
    items = [
        *_named_round("s1", "sql", "A" * 1000),
        *_named_round("t1", "ping", "ok"),
        *_named_round("t2", "ping", "ok"),
        _assistant_reply(),
    ]
    assert apply_tool_result_clearing(items, keep_tool_uses=1, clear_at_least_chars=0) is items


def test_apply_keeps_outputs_followed_only_by_a_user_message() -> None:
    """A run that stopped after its tools, then the user said "continue"."""
    items = [*_parallel_batch(6), {"role": "user", "content": "continue"}]
    assert apply_tool_result_clearing(items, keep_tool_uses=3, clear_at_least_chars=0) is items


def test_apply_noop_when_fewer_than_keep() -> None:
    items = [_tool_output("c1", "small")]
    assert apply_tool_result_clearing(items, keep_tool_uses=3) is items


def test_apply_skips_when_placeholder_would_be_larger() -> None:
    items = [_tool_output("c1", "tiny"), _assistant_reply()]
    cleared = apply_tool_result_clearing(items, keep_tool_uses=0, clear_at_least_chars=0)
    assert cleared[0]["output"] == "tiny"


def test_apply_rejects_negative_keep() -> None:
    with pytest.raises(ValueError, match="keep_tool_uses"):
        apply_tool_result_clearing([], keep_tool_uses=-1)


def test_apply_waits_until_combined_savings_reach_threshold() -> None:
    items = [
        _tool_call("c1", "sql"),
        _tool_output("c1", "X" * 2000),
        _tool_call("c2", "sql"),
        _tool_output("c2", "Y" * 2000),
        _tool_call("c3", "sql"),
        _tool_output("c3", RECENT),
        _assistant_reply(),
    ]

    assert apply_tool_result_clearing(items, keep_tool_uses=1, clear_at_least_chars=5000) is items
    cleared = apply_tool_result_clearing(items, keep_tool_uses=1, clear_at_least_chars=3000)
    assert cleared is not items
    assert cleared[1]["output"].startswith("[Tool result cleared")
    assert cleared[3]["output"].startswith("[Tool result cleared")
    assert cleared[5]["output"] == RECENT


def test_apply_rejects_negative_clear_at_least() -> None:
    with pytest.raises(ValueError, match="clear_at_least_chars"):
        apply_tool_result_clearing([], keep_tool_uses=3, clear_at_least_chars=-1)


def test_apply_rejects_negative_clear_above_input_tokens() -> None:
    with pytest.raises(ValueError, match="clear_above_input_tokens"):
        apply_tool_result_clearing([], keep_tool_uses=3, clear_above_input_tokens=-1)


def test_estimate_input_tokens_counts_instructions_and_items() -> None:
    """The system prompt, skills and data-universe block live in
    ``instructions``, so occupancy that ignored them would under-count the
    largest fixed part of the request.
    """
    items = [_tool_output("c1", "X" * 1000)]
    items_only = estimate_input_tokens(items)
    with_instructions = estimate_input_tokens(items, "Y" * 7000)
    assert with_instructions - items_only == int(7000 / CHARS_PER_TOKEN)


def test_occupancy_trigger_clears_when_savings_threshold_is_not_met() -> None:
    """The issue #424 case: a request already near the window must be cleared
    even though the clearable pile is far below ``clear_at_least_chars``.
    """
    items = [
        _tool_call("c1", "sql"),
        _tool_output("c1", "X" * 20_000),
        _tool_call("c2", "sql"),
        _tool_output("c2", RECENT),
    ]
    # Prose dominates occupancy: a large ``instructions`` stands in for the
    # long discussion history that no amount of tool clearing would recover.
    instructions = "Z" * 400_000

    assert (
        apply_tool_result_clearing(
            items,
            keep_tool_uses=1,
            clear_at_least_chars=300_000,
            clear_above_input_tokens=0,
            instructions=instructions,
        )
        is items
    )

    cleared = apply_tool_result_clearing(
        items,
        keep_tool_uses=1,
        clear_at_least_chars=300_000,
        clear_above_input_tokens=100_000,
        instructions=instructions,
    )
    assert cleared is not items
    assert cleared[1]["output"].startswith("[Tool result cleared")
    assert cleared[3]["output"] == RECENT


def test_occupancy_trigger_clears_every_eligible_result_at_once() -> None:
    """One large pass breaks the prompt cache once, not on every later call."""
    items = [
        _tool_call("c1", "sql"),
        _tool_output("c1", "X" * 7000),
        _tool_call("c2", "sql"),
        _tool_output("c2", "Y" * 7000),
        _tool_call("c3", "sql"),
        _tool_output("c3", RECENT),
        _assistant_reply(),
    ]
    cleared = apply_tool_result_clearing(
        items, keep_tool_uses=1, clear_at_least_chars=300_000, clear_above_input_tokens=5_000
    )
    assert cleared[1]["output"].startswith("[Tool result cleared")
    assert cleared[3]["output"].startswith("[Tool result cleared")
    assert cleared[5]["output"] == RECENT


def _round(call_id: str, output: str) -> list[dict[str, Any]]:
    return [_tool_call(call_id, "sql"), _tool_output(call_id, output), _assistant_reply()]


def _send(run_filter: Any, items: list[Any]) -> list[Any]:
    from agents.run_config import ModelInputData

    return list(run_filter(_FakeCallModelData(ModelInputData(input=items, instructions=""))).input)


class TestClearingWithinARun:
    """Between passes every call sends the same prefix, so the prompt cache keeps hitting."""

    _SETTINGS = ToolResultClearingSettings(
        keep_tool_uses=1, clear_at_least_chars=300_000, clear_above_input_tokens=5_000
    )

    def test_cleared_outputs_stay_cleared_and_aged_ones_wait_for_the_next_pass(self) -> None:
        run_filter = build_call_model_input_filter(self._SETTINGS)
        items = [*_round("c1", "X" * 7000), *_round("c2", "Y" * 7000), *_round("c3", "W" * 3000)]
        first = _send(run_filter, items)
        assert [first[i]["output"][:12] for i in (1, 4)] == ["[Tool result"] * 2
        assert first[7]["output"] == "W" * 3000

        second = _send(run_filter, [*items, *_round("c4", "Z" * 3000)])
        assert second[: len(first)] == first

    def test_a_new_pass_runs_once_the_cleared_request_is_over_again(self) -> None:
        run_filter = build_call_model_input_filter(self._SETTINGS)
        items = [*_round("c1", "X" * 7000), *_round("c2", "Y" * 7000), *_round("c3", "W" * 3000)]
        _send(run_filter, items)
        third = _send(run_filter, [*items, *_round("c4", "Z" * 3000), *_round("c5", "V" * 8000)])
        assert [third[i]["output"][:12] for i in (1, 4, 7, 10)] == ["[Tool result"] * 4
        assert third[13]["output"] == "V" * 8000

    def test_each_run_starts_without_cleared_outputs(self) -> None:
        items = [*_round("c1", "X" * 7000), *_round("c2", "Y" * 7000), *_round("c3", "W" * 3000)]
        _send(build_call_model_input_filter(self._SETTINGS), items)
        small = [*_round("c1", "X" * 300), *_round("c2", RECENT)]
        assert _send(build_call_model_input_filter(self._SETTINGS), small) == small


def test_occupancy_trigger_does_not_fire_below_threshold() -> None:
    items = [
        _tool_output("c1", "X" * 5000),
        _tool_output("c2", RECENT),
        _assistant_reply(),
    ]
    assert (
        apply_tool_result_clearing(
            items,
            keep_tool_uses=1,
            clear_at_least_chars=300_000,
            clear_above_input_tokens=100_000,
        )
        is items
    )


def test_estimate_input_tokens_counts_tool_schemas() -> None:
    """Tool definitions are billed on every call but are not in the items."""
    items = [_tool_output("c1", "X" * 1000)]
    assert estimate_input_tokens(items, None, 7000) - estimate_input_tokens(items) == int(7000 / CHARS_PER_TOKEN)


def test_occupancy_trigger_counts_tool_schemas() -> None:
    items = [
        _tool_call("c1", "sql"),
        _tool_output("c1", "X" * 5000),
        _tool_call("c2", "sql"),
        _tool_output("c2", RECENT),
        _assistant_reply(),
    ]
    kwargs: dict[str, Any] = {"keep_tool_uses": 1, "clear_at_least_chars": 300_000, "clear_above_input_tokens": 3_500}
    assert apply_tool_result_clearing(items, **kwargs) is items
    cleared = apply_tool_result_clearing(items, **kwargs, tool_schema_chars=3_000)
    assert cleared[1]["output"].startswith("[Tool result cleared")


def test_filter_records_tool_schemas() -> None:
    from agents.run_config import ModelInputData

    items: list[Any] = [_tool_output("c1", "X" * 700)]
    recorder = OccupancyRecorder()
    run_filter = build_call_model_input_filter(ToolResultClearingSettings(enabled=False), recorder, 3_500)
    run_filter(_FakeCallModelData(ModelInputData(input=items, instructions="SYSTEM")))
    assert recorder.estimated_input_tokens == estimate_input_tokens(items, "SYSTEM", 3_500)


class _ListingAgent:
    def __init__(self, tools: list[Any] | Exception) -> None:
        self._tools = tools

    async def get_all_tools(self, run_context: Any) -> list[Any]:
        if isinstance(self._tools, Exception):
            raise self._tools
        return self._tools


@pytest.mark.asyncio
async def test_tool_schema_chars_counts_name_description_and_schema() -> None:
    schema = {"type": "object", "properties": {"sql": {"type": "string"}}}
    tools = [
        SimpleNamespace(name="query", description="Run SQL", params_json_schema=schema),
        SimpleNamespace(name="bare", description=None, params_json_schema=None),
    ]
    expected = len("query") + len("Run SQL") + len(json.dumps(schema)) + len("bare")
    assert await tool_schema_chars(_ListingAgent(tools), None) == expected


@pytest.mark.asyncio
async def test_tool_schema_chars_is_zero_when_listing_fails() -> None:
    assert await tool_schema_chars(_ListingAgent(RuntimeError("mcp down")), None) == 0


def test_occupancy_trigger_warns_when_clearing_cannot_help(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Clearing only reaches tool output.  When the request stays over the
    threshold afterwards, the growth is prose and the operator needs to know
    before the provider's hard error rather than after it.
    """
    items = [
        _tool_call("c1", "sql"),
        _tool_output("c1", "X" * 10_000),
        _tool_call("c2", "sql"),
        _tool_output("c2", RECENT),
        _assistant_reply(),
    ]
    with caplog.at_level("WARNING"):
        apply_tool_result_clearing(
            items,
            keep_tool_uses=1,
            clear_above_input_tokens=100_000,
            instructions="Z" * 400_000,
        )
    assert "clearing alone cannot bound it" in caplog.text


def test_filter_never_rewrites_instructions() -> None:
    """The cached static prefix -- prompt, skills, data universe -- lives in
    ``instructions``, not in the message list.  Rewriting it would turn every
    request into a full-price cache write, so the filter must pass it through
    byte-identically.
    """
    from agents.run_config import ModelInputData

    instructions = "SYSTEM PROMPT\n\n## Data Universe\n| view |\n| --- |\n| a |"
    items: list[Any] = []
    for index in range(1, 6):
        items.append(_tool_call(f"c{index}", "sql"))
        items.append(_tool_output(f"c{index}", "X" * 5000))

    # ``clear_at_least_chars=0`` so a pass definitely fires: this test is about
    # what clearing must not touch, which is vacuous if nothing is cleared.
    run_filter = build_call_model_input_filter(ToolResultClearingSettings(keep_tool_uses=3, clear_at_least_chars=0))
    result = run_filter(_FakeCallModelData(ModelInputData(input=items, instructions=instructions)))

    assert result.instructions == instructions
    # The original list is an input to the SDK's own bookkeeping; clearing
    # must copy rather than mutate it.
    assert all(len(item["output"]) == 5000 for item in items if item["type"] == "function_call_output")
    assert [item for item in result.input if item["type"] == "function_call"] == [
        item for item in items if item["type"] == "function_call"
    ]


def test_run_config_for_disabled() -> None:
    assert run_config_for({"TOOL_RESULT_CLEARING_ENABLED": False}) is None


def test_run_config_for_enabled() -> None:
    run_config = run_config_for({"TOOL_RESULT_CLEARING_ENABLED": True})
    assert run_config is not None
    assert run_config.call_model_input_filter is not None
