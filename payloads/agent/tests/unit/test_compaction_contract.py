"""Tests for ``spec.runtime.context.compaction`` (#637).

Compaction ships on by default, so the derived values must keep every
existing ``payload.yml`` valid: the toggle follows chat history and the
trigger moves above a raised clearing threshold instead of failing it.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from payloads.agent.config import AgentConfig
from payloads.agent.contracts import (
    DEFAULT_CLEAR_ABOVE_INPUT_TOKENS,
    MAX_COMPACTION_INSTRUCTIONS_CHARS,
    AgentPayloadSpec,
)


def _spec(context: dict[str, Any], ui: dict[str, Any] | None = None) -> AgentPayloadSpec:
    return AgentPayloadSpec.model_validate({"runtime": {"context": context}, "ui": ui or {}})


class TestCompactionEnabled:
    def test_follows_chat_history_by_default(self) -> None:
        assert AgentPayloadSpec().compaction_enabled() is True
        assert _spec({}, {"chat_history": {"enabled": False}}).compaction_enabled() is False

    def test_explicit_off_wins(self) -> None:
        assert _spec({"compaction": {"enabled": False}}).compaction_enabled() is False

    def test_explicit_on_requires_chat_history(self) -> None:
        with pytest.raises(ValidationError, match="requires spec.ui.chat_history.enabled"):
            _spec({"compaction": {"enabled": True}}, {"chat_history": {"enabled": False}})


class TestCompactionTrigger:
    def test_default(self) -> None:
        assert AgentPayloadSpec().runtime.context.compaction_trigger_input_tokens() == 120_000

    def test_derived_below_a_lowered_clearing_threshold(self) -> None:
        spec = _spec({"tool_result_clearing": {"clear_above_input_tokens": 60_000}})
        assert spec.runtime.context.compaction_trigger_input_tokens() == 40_000

    def test_a_raised_clearing_threshold_keeps_the_default(self) -> None:
        spec = _spec({"tool_result_clearing": {"clear_above_input_tokens": 190_000}})
        assert spec.runtime.context.compaction_trigger_input_tokens() == 120_000

    def test_authored_value_wins(self) -> None:
        spec = _spec({"compaction": {"trigger_input_tokens": 110_000}})
        assert spec.runtime.context.compaction_trigger_input_tokens() == 110_000

    @pytest.mark.parametrize("trigger", [DEFAULT_CLEAR_ABOVE_INPUT_TOKENS, 180_000])
    def test_authored_value_must_stay_below_clearing(self, trigger: int) -> None:
        with pytest.raises(ValidationError, match="must stay below"):
            _spec({"compaction": {"trigger_input_tokens": trigger}})

    @pytest.mark.parametrize(
        "clearing",
        [{"enabled": False}, {"clear_above_input_tokens": 0}],
    )
    def test_any_trigger_allowed_when_clearing_never_fires(self, clearing: dict[str, Any]) -> None:
        spec = _spec({"tool_result_clearing": clearing, "compaction": {"trigger_input_tokens": 50_000}})
        assert spec.runtime.context.compaction_trigger_input_tokens() == 50_000

    def test_clearing_default_is_shared_with_agent_config(self) -> None:
        assert AgentConfig().tool_result_clearing_clear_above_input_tokens == DEFAULT_CLEAR_ABOVE_INPUT_TOKENS


class TestCompactionFields:
    @pytest.mark.parametrize(
        "compaction",
        [
            {"keep_recent_turns": 0},
            {"trigger_input_tokens": 0},
            {"instructions": "x" * (MAX_COMPACTION_INSTRUCTIONS_CHARS + 1)},
            {"unknown": True},
        ],
    )
    def test_invalid_values_are_rejected(self, compaction: dict[str, Any]) -> None:
        with pytest.raises(ValidationError):
            _spec({"compaction": compaction})
