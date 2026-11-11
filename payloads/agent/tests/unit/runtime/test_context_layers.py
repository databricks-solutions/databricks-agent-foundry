"""Tests for building the plan and memory of a request, both as the signed-in user."""

from __future__ import annotations

import asyncio
from functools import partial
from typing import Any
from unittest.mock import AsyncMock, patch

from payloads.agent.contracts import AgentMemoryRuntime, AgentPlanRuntime
from payloads.agent.runtime.context_layers import ContextLayers, prepare_context_layers
from payloads.agent.tests.unit.runtime.conftest import ALICE, CHAT

_MEMORY = AgentMemoryRuntime(enabled=True)
_PLAN = AgentPlanRuntime(enabled=True)
_HEADERS = {"x-forwarded-user": ALICE}


def _prepare(
    config: dict[str, Any], *, actor: str | None = ALICE, session_id: str | None = CHAT
) -> tuple[ContextLayers, AsyncMock, AsyncMock]:
    headers = {"x-forwarded-user": actor} if actor else {}
    with (
        patch("payloads.agent.runtime.context_layers.get_forwarded_headers", return_value=headers),
        patch("payloads.agent.runtime.context_layers.prepare_memory", new_callable=AsyncMock) as memory,
        patch("payloads.agent.runtime.context_layers.prepare_plan", new_callable=AsyncMock) as plan,
    ):
        layers = asyncio.run(prepare_context_layers(config, session_id=session_id))
        return layers, memory, plan


async def _meet_at(barrier: asyncio.Barrier, *_args: object, **_kwargs: object) -> object:
    """Return once every party has called in; times out when the callers run one after another."""
    await asyncio.wait_for(barrier.wait(), timeout=1)
    return object()


class TestPrepareContextLayers:
    def test_off_without_settings(self) -> None:
        layers, memory, plan = _prepare({})
        assert layers == ContextLayers()
        memory.assert_not_called()
        plan.assert_not_called()

    def test_off_without_a_trusted_user(self) -> None:
        layers, memory, plan = _prepare({"MEMORY": _MEMORY, "PLAN": _PLAN}, actor=None)
        assert layers == ContextLayers()
        memory.assert_not_called()
        plan.assert_not_called()

    def test_memory_is_read_as_the_user(self) -> None:
        layers, memory, _ = _prepare({"MEMORY": _MEMORY})
        assert layers.memory is memory.return_value
        memory.assert_awaited_once_with(_MEMORY, _HEADERS)

    def test_the_plan_alone(self) -> None:
        layers, memory, plan = _prepare({"PLAN": _PLAN})
        memory.assert_not_called()
        assert layers.plan is plan.return_value
        plan.assert_awaited_once_with(_PLAN, CHAT, _HEADERS, memory=False)

    def test_the_plan_hands_lasting_facts_to_memory_when_it_is_on(self) -> None:
        _, _, plan = _prepare({"MEMORY": _MEMORY, "PLAN": _PLAN})
        assert plan.call_args.kwargs == {"memory": True}

    def test_no_plan_without_a_conversation(self) -> None:
        layers, _, plan = _prepare({"MEMORY": _MEMORY, "PLAN": _PLAN}, session_id=None)
        assert layers.plan is None and layers.memory is not None
        plan.assert_not_called()

    def test_memory_and_plan_load_concurrently(self) -> None:
        meet = partial(_meet_at, asyncio.Barrier(2))
        with (
            patch("payloads.agent.runtime.context_layers.get_forwarded_headers", return_value=_HEADERS),
            patch("payloads.agent.runtime.context_layers.prepare_memory", side_effect=meet),
            patch("payloads.agent.runtime.context_layers.prepare_plan", side_effect=meet),
        ):
            layers = asyncio.run(prepare_context_layers({"MEMORY": _MEMORY, "PLAN": _PLAN}, session_id=CHAT))

        assert layers.memory is not None and layers.plan is not None
