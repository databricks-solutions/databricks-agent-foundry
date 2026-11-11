"""Tests for payloads/agent/runtime/a2a_adapter.py -- A2A protocol adapter."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_foundry.contracts.a2a import (
    A2AAgentCapabilities,
    A2AAgentCardConfig,
    A2AAgentProvider,
    A2AAgentSkill,
    A2AConfig,
)
from payloads.agent.runtime.a2a_adapter import (
    _FALLBACK_CARD,
    FoundryAgentExecutor,
    _resolve_card_and_flags,
    build_agent_card,
    register_a2a_routes,
)


def _skill(skill_id: str = "ask", name: str = "Ask") -> A2AAgentSkill:
    return A2AAgentSkill(id=skill_id, name=name, description="d")


def _card(**overrides: Any) -> A2AAgentCardConfig:
    """Build a minimal valid card config, allowing overrides per test."""
    kwargs = {
        "name": "Test Agent",
        "description": "test",
        "version": "1.0.0",
        "skills": [_skill()],
    }
    kwargs.update(overrides)
    return A2AAgentCardConfig(**kwargs)


async def _fake_stream(*chunks: str) -> AsyncGenerator[str, None]:
    """Helper: async generator that yields the given chunks."""
    for c in chunks:
        yield c


# ---------------------------------------------------------------------------
# build_agent_card -- contract-to-proto builder is the most-tested surface
# because it codifies the user-visible contract <-> wire shape mapping.
# ---------------------------------------------------------------------------


class TestBuildAgentCard:
    def test_required_fields_map_through(self) -> None:
        card_proto = build_agent_card(_card(name="X", description="d", version="2.0.0"))
        assert card_proto.name == "X"
        assert card_proto.description == "d"
        assert card_proto.version == "2.0.0"

    def test_default_modes_propagate(self) -> None:
        card_proto = build_agent_card(_card())
        assert list(card_proto.default_input_modes) == ["text"]
        assert list(card_proto.default_output_modes) == ["text"]

    def test_capabilities_streaming_default_true(self) -> None:
        card_proto = build_agent_card(_card())
        assert card_proto.capabilities.streaming is True
        assert card_proto.capabilities.push_notifications is False

    def test_capabilities_overrides_propagate(self) -> None:
        card_proto = build_agent_card(
            _card(
                capabilities=A2AAgentCapabilities(streaming=False, push_notifications=True),
            )
        )
        assert card_proto.capabilities.streaming is False
        assert card_proto.capabilities.push_notifications is True

    def test_skill_mode_inheritance_when_unset(self) -> None:
        """Per-skill input_modes / output_modes default to None on the
        contract; the builder substitutes the card-level defaults.
        Pinned to keep the authored YAML minimal when every skill uses
        the same modes."""
        card_proto = build_agent_card(
            _card(
                default_input_modes=["text", "audio"],
                default_output_modes=["text"],
                skills=[_skill("s1")],
            )
        )
        s = card_proto.skills[0]
        assert list(s.input_modes) == ["text", "audio"]
        assert list(s.output_modes) == ["text"]

    def test_skill_mode_override_wins(self) -> None:
        """An explicit per-skill mode must override the card default."""
        explicit = A2AAgentSkill(
            id="s1",
            name="S1",
            description="d",
            input_modes=["audio"],
            output_modes=["audio"],
        )
        card_proto = build_agent_card(
            _card(default_input_modes=["text"], default_output_modes=["text"], skills=[explicit])
        )
        s = card_proto.skills[0]
        assert list(s.input_modes) == ["audio"]
        assert list(s.output_modes) == ["audio"]

    def test_skill_tags_and_examples_propagate(self) -> None:
        sk = A2AAgentSkill(
            id="s1",
            name="S1",
            description="d",
            tags=["finance", "portfolio"],
            examples=["q1?", "q2?"],
        )
        card_proto = build_agent_card(_card(skills=[sk]))
        s = card_proto.skills[0]
        assert list(s.tags) == ["finance", "portfolio"]
        assert list(s.examples) == ["q1?", "q2?"]

    def test_provider_omitted_when_none(self) -> None:
        """When the contract carries no provider, the proto field must
        not be populated -- proto3 reads an unset message as the default
        instance, so we check via HasField rather than truthiness."""
        card_proto = build_agent_card(_card(provider=None))
        assert not card_proto.HasField("provider")

    def test_provider_omitted_when_both_fields_empty(self) -> None:
        """Defensive: an empty provider object (both fields None) is
        equivalent to no provider at all and must not be propagated."""
        card_proto = build_agent_card(_card(provider=A2AAgentProvider()))
        assert not card_proto.HasField("provider")

    def test_provider_emitted_when_partial(self) -> None:
        card_proto = build_agent_card(_card(provider=A2AAgentProvider(organization="Acme")))
        assert card_proto.HasField("provider")
        assert card_proto.provider.organization == "Acme"
        assert card_proto.provider.url == ""

    def test_provider_emitted_when_full(self) -> None:
        card_proto = build_agent_card(_card(provider=A2AAgentProvider(organization="Acme", url="https://acme")))
        assert card_proto.provider.organization == "Acme"
        assert card_proto.provider.url == "https://acme"

    def test_documentation_url_propagates_when_set(self) -> None:
        card_proto = build_agent_card(_card(documentation_url="https://docs"))
        assert card_proto.documentation_url == "https://docs"

    def test_icon_url_propagates_when_set(self) -> None:
        card_proto = build_agent_card(_card(icon_url="https://icon"))
        assert card_proto.icon_url == "https://icon"

    def test_supported_interface_relative_url_by_default(self) -> None:
        card_proto = build_agent_card(_card())
        iface = card_proto.supported_interfaces[0]
        assert iface.protocol_binding == "JSONRPC"
        assert iface.protocol_version == "1.0"
        assert iface.url == "/api/a2a"

    def test_supported_interface_absolute_url_when_base_url_given(self) -> None:
        card_proto = build_agent_card(_card(), base_url="https://my-app.databricks.app")
        iface = card_proto.supported_interfaces[0]
        assert iface.url == "https://my-app.databricks.app/api/a2a"

    def test_supported_interfaces_default_is_v1_only(self) -> None:
        """With ``enable_v0_3_compat`` left at its default (False) the
        card advertises a single v1.0 JSON-RPC interface. Pre-1.0
        clients are not catered for."""
        card_proto = build_agent_card(_card())
        assert len(card_proto.supported_interfaces) == 1
        iface = card_proto.supported_interfaces[0]
        assert iface.protocol_binding == "JSONRPC"
        assert iface.protocol_version == "1.0"

    def test_supported_interfaces_includes_v0_3_when_enabled(self) -> None:
        """With the compat flag on, the card advertises both v1.0 and
        v0.3 JSON-RPC interfaces at the same URL, in that order. Order
        matters: v1.0 clients pick the first compatible interface, so
        keeping v1.0 first means modern clients keep using the modern
        path even when compat is on."""
        card_proto = build_agent_card(
            _card(),
            base_url="https://my-app.databricks.app",
            enable_v0_3_compat=True,
        )
        assert len(card_proto.supported_interfaces) == 2
        v10, v03 = card_proto.supported_interfaces
        assert v10.protocol_binding == "JSONRPC"
        assert v10.protocol_version == "1.0"
        assert v03.protocol_binding == "JSONRPC"
        assert v03.protocol_version == "0.3"
        assert v10.url == v03.url == "https://my-app.databricks.app/api/a2a"

    def test_multiple_skills_preserve_order(self) -> None:
        card_proto = build_agent_card(_card(skills=[_skill("a"), _skill("b"), _skill("c")]))
        assert [s.id for s in card_proto.skills] == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# _resolve_card_and_flags -- loader / fallback logic (card + compat flag tuple)
# ---------------------------------------------------------------------------


class TestResolveCardAndFlags:
    def test_returns_authored_card_with_compat_default_off(self) -> None:
        """When the instance authors a card but does not set the
        compat flag, the resolver must default ``enable_v0_3_compat``
        to ``False`` -- v0.3 advertising is strictly opt-in."""
        custom = _card(name="Custom")
        with (
            patch(
                "payloads.agent.runtime.runtime_config.load_agent_config_and_vars",
                return_value=({"A2A": A2AConfig(card=custom)}, {}),
            ),
            patch("payloads.agent.runtime.request_context.get_workspace_client", return_value=MagicMock()),
        ):
            resolved_card, enable_v0_3_compat = _resolve_card_and_flags()
        assert resolved_card is custom
        assert enable_v0_3_compat is False

    def test_returns_authored_card_with_compat_true_when_set(self) -> None:
        custom = _card(name="Custom")
        with (
            patch(
                "payloads.agent.runtime.runtime_config.load_agent_config_and_vars",
                return_value=(
                    {"A2A": A2AConfig(card=custom, enable_v0_3_compat=True)},
                    {},
                ),
            ),
            patch("payloads.agent.runtime.request_context.get_workspace_client", return_value=MagicMock()),
        ):
            resolved_card, enable_v0_3_compat = _resolve_card_and_flags()
        assert resolved_card is custom
        assert enable_v0_3_compat is True

    def test_falls_back_when_a2a_key_missing(self, caplog: pytest.LogCaptureFixture) -> None:
        with (
            patch(
                "payloads.agent.runtime.runtime_config.load_agent_config_and_vars",
                return_value=({}, {}),
            ),
            patch("payloads.agent.runtime.request_context.get_workspace_client", return_value=MagicMock()),
        ):
            with caplog.at_level("WARNING", logger="payloads.agent.runtime.a2a_adapter"):
                resolved_card, enable_v0_3_compat = _resolve_card_and_flags()
        assert resolved_card is _FALLBACK_CARD
        assert enable_v0_3_compat is False
        expected = "no 'integrations.a2a.card' block in payloads/agent/payload.yml"
        assert any(expected in r.getMessage() for r in caplog.records)

    def test_falls_back_when_card_is_none(self) -> None:
        with (
            patch(
                "payloads.agent.runtime.runtime_config.load_agent_config_and_vars",
                return_value=({"A2A": A2AConfig(card=None)}, {}),
            ),
            patch("payloads.agent.runtime.request_context.get_workspace_client", return_value=MagicMock()),
        ):
            resolved_card, enable_v0_3_compat = _resolve_card_and_flags()
        assert resolved_card is _FALLBACK_CARD
        assert enable_v0_3_compat is False

    def test_falls_back_when_loader_raises(self, caplog: pytest.LogCaptureFixture) -> None:
        """The A2A adapter must not bring the whole app down when the
        runtime config cannot be loaded -- fall back to the generic
        card with a WARNING."""
        with patch(
            "payloads.agent.runtime.request_context.get_workspace_client",
            side_effect=RuntimeError("ws unavailable"),
        ):
            with caplog.at_level("WARNING", logger="payloads.agent.runtime.a2a_adapter"):
                resolved_card, enable_v0_3_compat = _resolve_card_and_flags()
        assert resolved_card is _FALLBACK_CARD
        assert enable_v0_3_compat is False
        assert any("failed to load agent config" in r.getMessage() for r in caplog.records)


class TestFallbackCard:
    """The fallback card preserves pre-typed-config behaviour exactly,
    so an instance that hasn't migrated keeps shipping the same shape."""

    def test_fallback_matches_pre_typed_config_shape(self) -> None:
        assert _FALLBACK_CARD.name == "Foundry Agent"
        assert _FALLBACK_CARD.description == "Databricks AI Agent exposed via A2A protocol"
        assert _FALLBACK_CARD.version == "1.0.0"
        assert len(_FALLBACK_CARD.skills) == 1
        skill = _FALLBACK_CARD.skills[0]
        assert skill.id == "ask_agent"
        assert skill.name == "Ask Agent"
        assert skill.tags == ["general"]


# ---------------------------------------------------------------------------
# Executor -- unchanged from pre-refactor; tests preserved.
# ---------------------------------------------------------------------------


class TestFoundryAgentExecutor:
    @pytest.mark.asyncio
    @patch("payloads.agent.runtime.a2a_adapter.run_agent_streamed")
    async def test_execute_emits_correct_event_sequence(self, mock_stream: MagicMock) -> None:
        mock_stream.return_value = _fake_stream("Agent says hello")

        executor = FoundryAgentExecutor()
        context = MagicMock()
        context.current_task = None
        context.task_id = "task-1"
        context.context_id = "ctx-1"
        context.get_user_input.return_value = "Hello agent"

        event_queue = MagicMock()
        event_queue.enqueue_event = AsyncMock()

        await executor.execute(context, event_queue)

        calls = event_queue.enqueue_event.call_args_list
        # Task + WORKING status + 1 artifact chunk + COMPLETED status = 4
        assert len(calls) == 4
        mock_stream.assert_called_once_with("Hello agent", source="a2a")

    @pytest.mark.asyncio
    @patch("payloads.agent.runtime.a2a_adapter.run_agent_streamed")
    async def test_execute_streams_multiple_chunks(self, mock_stream: MagicMock) -> None:
        mock_stream.return_value = _fake_stream("chunk1", "chunk2", "chunk3")

        executor = FoundryAgentExecutor()
        context = MagicMock()
        context.current_task = None
        context.task_id = "task-1"
        context.context_id = "ctx-1"
        context.get_user_input.return_value = "Hello"

        event_queue = MagicMock()
        event_queue.enqueue_event = AsyncMock()

        await executor.execute(context, event_queue)

        calls = event_queue.enqueue_event.call_args_list
        # Task + WORKING status + 3 artifact chunks + COMPLETED status = 6
        assert len(calls) == 6

    @pytest.mark.asyncio
    @patch("payloads.agent.runtime.a2a_adapter.run_agent_streamed")
    async def test_execute_marks_error_as_failed(self, mock_stream: MagicMock) -> None:
        mock_stream.return_value = _fake_stream("Error: something went wrong")

        executor = FoundryAgentExecutor()
        context = MagicMock()
        context.current_task = None
        context.task_id = "task-2"
        context.context_id = "ctx-2"
        context.get_user_input.return_value = "fail"

        event_queue = MagicMock()
        event_queue.enqueue_event = AsyncMock()

        await executor.execute(context, event_queue)

        last_event = event_queue.enqueue_event.call_args_list[-1][0][0]
        from a2a.types.a2a_pb2 import TaskState

        assert last_event.status.state == TaskState.TASK_STATE_FAILED

    @pytest.mark.asyncio
    async def test_cancel_raises_not_implemented(self) -> None:
        executor = FoundryAgentExecutor()
        with pytest.raises(NotImplementedError):
            await executor.cancel(MagicMock(), MagicMock())


# ---------------------------------------------------------------------------
# register_a2a_routes -- end-to-end smoke check via FastAPI TestClient.
# ---------------------------------------------------------------------------


class TestRegisterA2ARoutes:
    def test_adds_routes_to_app(self) -> None:
        """Pre-refactor regression: at least the JSONRPC + agent-card
        routes must be added.  Patches the loader so the test runs
        offline."""
        from fastapi import FastAPI

        app = FastAPI()
        initial_routes = len(app.routes)

        with patch(
            "payloads.agent.runtime.a2a_adapter._resolve_card_and_flags",
            return_value=(_FALLBACK_CARD, False),
        ):
            register_a2a_routes(app)

        assert len(app.routes) > initial_routes

    def test_agent_card_json_endpoint_serves_authored_card(self) -> None:
        """End-to-end smoke check: the authored card shape lands on the
        wire at /api/.well-known/agent-card.json.  Verifies the full
        chain (contract -> builder -> proto -> a2a-sdk route -> HTTP
        response) for the user-visible bits."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        authored = A2AAgentCardConfig(
            name="Test Authored Agent",
            description="end-to-end smoke",
            version="2.1.0",
            provider=A2AAgentProvider(organization="Acme"),
            skills=[
                A2AAgentSkill(
                    id="explore",
                    name="Explore",
                    description="Explore data",
                    tags=["data"],
                    examples=["What catalogs are available?"],
                )
            ],
        )

        app = FastAPI()
        with patch(
            "payloads.agent.runtime.a2a_adapter._resolve_card_and_flags",
            return_value=(authored, False),
        ):
            register_a2a_routes(app)

        client = TestClient(app)
        response = client.get("/api/.well-known/agent-card.json")
        assert response.status_code == 200
        body = response.json()
        assert body["name"] == "Test Authored Agent"
        assert body["version"] == "2.1.0"
        # The a2a-sdk serialises proto field names in camelCase on the
        # wire (e.g. ``defaultInputModes``); keep this assertion narrow
        # so we don't pin the SDK's serialisation format in the test
        # but still confirm the skill content survives the round-trip.
        skills_blob = json.dumps(body)
        assert "explore" in skills_blob
        assert "What catalogs are available?" in skills_blob

    def test_register_routes_forwards_v0_3_compat_to_sdk(self) -> None:
        """Pin the contract that the resolved compat flag flows into
        the SDK's route factory. Regression guard: a future refactor
        that splits card-vs-route handling could silently drop the
        flag, advertising v0.3 in the card but failing v0.3 JSON-RPC
        payloads at runtime."""
        from fastapi import FastAPI

        app = FastAPI()
        with (
            patch(
                "payloads.agent.runtime.a2a_adapter._resolve_card_and_flags",
                return_value=(_FALLBACK_CARD, True),
            ),
            patch(
                "payloads.agent.runtime.a2a_adapter.create_jsonrpc_routes",
                return_value=[],
            ) as mock_rpc,
        ):
            register_a2a_routes(app)

        mock_rpc.assert_called_once()
        kwargs = mock_rpc.call_args.kwargs
        assert kwargs["enable_v0_3_compat"] is True
        assert kwargs["rpc_url"] == "/api/a2a"

    def test_register_routes_default_does_not_enable_v0_3_compat(self) -> None:
        """Symmetric to the above: with the flag off the SDK kwarg is
        explicitly False so v0.3 translation stays disabled."""
        from fastapi import FastAPI

        app = FastAPI()
        with (
            patch(
                "payloads.agent.runtime.a2a_adapter._resolve_card_and_flags",
                return_value=(_FALLBACK_CARD, False),
            ),
            patch(
                "payloads.agent.runtime.a2a_adapter.create_jsonrpc_routes",
                return_value=[],
            ) as mock_rpc,
        ):
            register_a2a_routes(app)

        mock_rpc.assert_called_once()
        assert mock_rpc.call_args.kwargs["enable_v0_3_compat"] is False
