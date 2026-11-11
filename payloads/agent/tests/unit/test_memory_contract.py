"""Tests for the plan and memory settings under ``spec.runtime.context``.

Both features ship off, so every existing ``payload.yml`` without them
stays valid; keys that moved or were removed fail with a message saying so.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from payloads.agent.contracts import MAX_MEMORY_INDEX_ENTRIES, AgentPayloadSpec


def _spec(memory: dict[str, Any], ui: dict[str, Any] | None = None) -> AgentPayloadSpec:
    return AgentPayloadSpec.model_validate({"runtime": {"context": {"memory": memory}}, "ui": ui or {}})


_NO_HISTORY = {"chat_history": {"enabled": False}}


class TestMemory:
    def test_off_by_default(self) -> None:
        assert AgentPayloadSpec().memory_enabled() is False

    def test_requires_chat_history(self) -> None:
        with pytest.raises(ValidationError, match="memory.enabled requires spec.ui.chat_history.enabled"):
            _spec({"enabled": True}, _NO_HISTORY)

    def test_off_without_chat_history(self) -> None:
        assert _spec({"enabled": False}, _NO_HISTORY).memory_enabled() is False


def _context(context: dict[str, Any], ui: dict[str, Any] | None = None) -> AgentPayloadSpec:
    return AgentPayloadSpec.model_validate({"runtime": {"context": context}, "ui": ui or {}})


class TestPlan:
    def test_off_by_default(self) -> None:
        spec = AgentPayloadSpec()
        assert (spec.plan_enabled(), spec.memory_enabled()) == (False, False)

    def test_on_without_memory(self) -> None:
        spec = _context({"plan": {"enabled": True}})
        assert (spec.plan_enabled(), spec.memory_enabled()) == (True, False)

    def test_requires_chat_history(self) -> None:
        with pytest.raises(ValidationError, match="plan.enabled requires spec.ui.chat_history.enabled"):
            _context({"plan": {"enabled": True}}, _NO_HISTORY)

    def test_max_chars_is_positive(self) -> None:
        with pytest.raises(ValidationError):
            _context({"plan": {"enabled": True, "max_chars": 0}})


class TestMemoryStoreRemoved:
    @pytest.mark.parametrize("store", ["my-agent-memory", None])
    def test_is_a_hard_error(self, store: str | None) -> None:
        with pytest.raises(ValidationError, match="memory_store was removed: memory is kept in the chat-history"):
            _context({"memory_store": store})


class TestMovedKeys:
    @pytest.mark.parametrize(
        ("memory", "where"),
        [
            ({"store": "abc"}, "kept in the chat-history Lakebase"),
            ({"plan": True}, "spec.runtime.context.plan"),
            ({"user": True}, "always the user's"),
            ({"chat": True}, "per-chat working state is the plan"),
        ],
    )
    def test_name_their_new_place(self, memory: dict[str, Any], where: str) -> None:
        with pytest.raises(ValidationError, match=where):
            _spec(memory)


class TestMemoryFields:
    @pytest.mark.parametrize(
        "memory",
        [
            {"max_entry_chars": 0},
            {"index_max_entries": 0},
            {"index_max_entries": MAX_MEMORY_INDEX_ENTRIES + 1},
            {"unknown": True},
        ],
    )
    def test_invalid_values_are_rejected(self, memory: dict[str, Any]) -> None:
        with pytest.raises(ValidationError):
            _spec(memory)
