"""Tests for payloads/agent/runtime/core.py -- the agent factory."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
from agents import FunctionTool

from payloads.agent.runtime.brick_mcp_servers import (
    _BrickSpecLoad,
    _clear_brick_specs_cache,
    build_mcp_servers,
)
from payloads.agent.runtime.core import create_agent
from payloads.agent.runtime.gateway_resources import UserTokenRequiredError
from payloads.agent.runtime.gateway_skills import GatewaySkills
from payloads.agent.runtime.history_search import AgentHistorySearch
from payloads.agent.runtime.instructions import resolve_identity_preamble
from payloads.agent.runtime.mcp_availability import UnavailableServer
from payloads.agent.runtime.memory import AgentMemory
from payloads.agent.runtime.plan import PLAN_HEADING, PlanState
from payloads.agent.runtime.plan_tools import AgentPlan
from payloads.agent.runtime.prompt_cache import CONTEXT_SENTINEL, USER_TAIL_SENTINEL
from payloads.agent.runtime.request_context import (
    resolve_tools_client,
)
from payloads.agent.runtime.sql_result_read import AgentResultReader
from payloads.agent.runtime.tool_builders import create_python_tools, create_tools

_ROWS = (("t.s.triage", "triage", "Sort tickets."),)


class TestCreateAgentInstructions:
    """The system prompt must be byte-stable across requests to be cached.

    The identity lives here rather than in the conversation history: a
    per-request stamp is only applied to the *newest* user turn, so on the
    next request that message arrives unstamped and invalidates the cache
    from that point on, taking the previous turn's tool results with it.

    Within the prompt it goes **last**, behind the sentinel, so the cache
    breakpoint can be placed in front of it.
    """

    def test_identity_is_appended_behind_the_context_sentinel(self) -> None:
        config = {
            "SYSTEM_PROMPT": "You are a helper.",
            "LLM_ENDPOINT_NAME": "test-model",
            "INJECT_TIME_USER": True,
            "_resolved_username": "alice@example.com",
        }
        instructions = cast("str", create_agent(config, tools=[]).instructions)

        body, separator, identity = instructions.partition(CONTEXT_SENTINEL)
        assert separator, "the context must be delimited so the cache layer can move it after the history"
        assert body.startswith("You are a helper.")
        assert identity.startswith("[Context:")
        assert "User: alice@example.com" in identity

    def test_everything_cacheable_precedes_the_identity(self) -> None:
        """The ordering *is* the cost saving: a different user must not
        re-write the prompt, the manuals or the data universe."""
        config = {
            "SYSTEM_PROMPT": "Base.",
            "LLM_ENDPOINT_NAME": "test-model",
            "INJECT_TIME_USER": True,
            "_resolved_username": "alice@example.com",
        }
        instructions = cast(
            "str", create_agent(config, tools=[], prompt_manuals="## Manuals", data_universe="## DU").instructions
        )

        body = instructions.split(CONTEXT_SENTINEL)[0]
        assert body == "Base.\n\n## Manuals\n\n## DU\n\n"

    def test_gateway_skill_index_precedes_the_context(self) -> None:
        config = {
            "SYSTEM_PROMPT": "Base.",
            "LLM_ENDPOINT_NAME": "test-model",
            "INJECT_TIME_USER": True,
            "_resolved_username": "alice@example.com",
        }
        skills = GatewaySkills(block="## Skills", rows=_ROWS)
        agent = create_agent(config, tools=[], data_universe="## DU", gateway_skills=skills)
        instructions = cast("str", agent.instructions)

        body, _, tail = instructions.partition(USER_TAIL_SENTINEL)
        user_tail, _, context = tail.partition(CONTEXT_SENTINEL)
        assert body == "Base.\n\n## Skills\n\n## DU\n\n"
        assert user_tail == f"{skills.index}\n\n"
        assert context.startswith("[Context:")

    def test_gateway_skill_index_keeps_its_sentinel_without_identity(self) -> None:
        config = {"SYSTEM_PROMPT": "Base.", "LLM_ENDPOINT_NAME": "test-model", "INJECT_TIME_USER": False}
        skills = GatewaySkills(rows=_ROWS)
        agent = create_agent(config, tools=[], gateway_skills=skills)
        assert agent.instructions == f"Base.\n\n{USER_TAIL_SENTINEL}{skills.index}"

    def test_memory_guidance_is_cached_and_the_index_is_context(self) -> None:
        config = {"SYSTEM_PROMPT": "Base.", "LLM_ENDPOINT_NAME": "test-model", "INJECT_TIME_USER": False}
        tool = MagicMock(spec=FunctionTool)
        tool.name = "_remember"
        memory = AgentMemory(tools=(tool,), guidance="## Using memory", index="## Memory")
        agent = create_agent(config, tools=[], memory=memory)

        assert agent.instructions == f"Base.\n\n## Using memory\n\n{CONTEXT_SENTINEL}## Memory"
        assert tool in agent.tools

    def test_the_plan_is_recited_last_in_its_latest_state(self) -> None:
        config = {"SYSTEM_PROMPT": "Base.", "LLM_ENDPOINT_NAME": "test-model", "INJECT_TIME_USER": False}
        state = PlanState()
        memory = AgentMemory(tools=(), guidance="## Using memory", index="")
        plan = AgentPlan(tools=(), guidance="## Planning", state=state)
        agent = create_agent(config, tools=[], memory=memory, plan=plan)
        assert callable(agent.instructions)

        assert asyncio.run(agent.get_system_prompt(MagicMock())) == "Base.\n\n## Using memory\n\n## Planning"
        state.saved("**Goal:** Q3\n1. [ ] Inventory")
        context = cast("str", asyncio.run(agent.get_system_prompt(MagicMock()))).partition(CONTEXT_SENTINEL)[2]
        assert context.startswith(f"{PLAN_HEADING}\n**Goal:** Q3\n1. [ ] Inventory")

    def test_history_search_guidance_follows_the_planning_guidance(self) -> None:
        config = {"SYSTEM_PROMPT": "Base.", "LLM_ENDPOINT_NAME": "test-model", "INJECT_TIME_USER": False}
        tool = MagicMock(spec=FunctionTool)
        tool.name = "_search_history"
        plan = AgentPlan(tools=(), guidance="## Planning", state=PlanState())
        history_search = AgentHistorySearch(tools=(tool,), guidance="## Earlier turns")
        agent = create_agent(config, tools=[], plan=plan, history_search=history_search)

        assert asyncio.run(agent.get_system_prompt(MagicMock())) == "Base.\n\n## Planning\n\n## Earlier turns"
        assert tool in agent.tools

    def test_result_reader_guidance_follows_the_history_guidance(self) -> None:
        config = {"SYSTEM_PROMPT": "Base.", "LLM_ENDPOINT_NAME": "test-model", "INJECT_TIME_USER": False}
        tool = MagicMock(spec=FunctionTool)
        tool.name = "_read_result"
        history_search = AgentHistorySearch(tools=(), guidance="## Earlier turns")
        result_reader = AgentResultReader(tools=(tool,), guidance="## Large SQL results")
        agent = create_agent(config, tools=[], history_search=history_search, result_reader=result_reader)

        assert asyncio.run(agent.get_system_prompt(MagicMock())) == "Base.\n\n## Earlier turns\n\n## Large SQL results"
        assert tool in agent.tools

    def test_chat_guidance_precedes_the_manuals(self) -> None:
        config = {"SYSTEM_PROMPT": "Base.", "LLM_ENDPOINT_NAME": "test-model", "INJECT_TIME_USER": False}
        agent = create_agent(config, tools=[], chat_guidance="## Images", prompt_manuals="## Manuals")

        assert agent.instructions == "Base.\n\n## Images\n\n## Manuals"

    def test_unavailable_mcp_note_is_in_the_uncached_tail(self) -> None:
        config = {"SYSTEM_PROMPT": "Base.", "LLM_ENDPOINT_NAME": "test-model", "INJECT_TIME_USER": False}
        agent = create_agent(config, tools=[], unavailable_mcp=(UnavailableServer("jira", "connect_failed"),))

        body, _, tail = cast("str", agent.instructions).partition(USER_TAIL_SENTINEL)
        assert body == "Base.\n\n"
        assert tail.startswith("## Unavailable tools")
        assert "jira" in tail

    def test_inject_disabled_uses_the_bare_prompt(self) -> None:
        config = {
            "SYSTEM_PROMPT": "You are a helper.",
            "LLM_ENDPOINT_NAME": "test-model",
            "INJECT_TIME_USER": False,
        }
        agent = create_agent(config, tools=[])
        assert agent.instructions == "You are a helper."

    def test_instructions_are_a_string_not_a_callable(self) -> None:
        """Without a plan to recite, nothing in the prompt changes between model calls."""
        config = {
            "SYSTEM_PROMPT": "You are a helper.",
            "LLM_ENDPOINT_NAME": "test-model",
            "INJECT_TIME_USER": True,
        }
        assert isinstance(create_agent(config, tools=[]).instructions, str)

    def test_include_usage_is_on_for_databricks_streams(self) -> None:
        """openai-agents only defaults include_usage for the official OpenAI client."""
        from agents.models.chatcmpl_helpers import ChatCmplHelpers
        from openai import AsyncOpenAI

        config = {
            "SYSTEM_PROMPT": "You are a helper.",
            "LLM_ENDPOINT_NAME": "databricks-claude-sonnet-4-5",
            "INJECT_TIME_USER": False,
        }
        agent = create_agent(config, tools=[])
        settings = agent.model_settings
        assert settings is not None
        assert settings.include_usage is True

        client = AsyncOpenAI(api_key="test", base_url="https://example.invalid/serving-endpoints")
        assert ChatCmplHelpers.is_openai(client) is False
        assert ChatCmplHelpers.get_stream_options_param(client, settings, stream=True) == {"include_usage": True}

    def test_init_agent_module_wires_autolog_helper(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from payloads.agent.runtime import runtime_config

        runtime_config._module_initialized = False
        client = object()
        configure = MagicMock()
        monkeypatch.setattr(
            "payloads.agent.runtime.prompt_cache.PromptCachedDatabricksOpenAI",
            lambda: client,
        )
        monkeypatch.setattr("agents.set_default_openai_client", MagicMock())
        monkeypatch.setattr("agents.set_default_openai_api", MagicMock())
        monkeypatch.setattr("payloads.agent.runtime.tracing.configure_openai_autolog", configure)

        runtime_config.init_agent_module()

        configure.assert_called_once_with(client)
        assert runtime_config._module_initialized is True
        runtime_config._module_initialized = False

    def test_everything_before_the_context_is_identical_across_calls(self) -> None:
        """Byte-stability is the whole point; assert it directly."""
        config = {
            "SYSTEM_PROMPT": "You are a helper.",
            "LLM_ENDPOINT_NAME": "test-model",
            "INJECT_TIME_USER": True,
            "_resolved_username": "carol@example.com",
        }
        first = cast("str", create_agent(config, tools=[]).instructions)
        second = cast("str", create_agent(config, tools=[]).instructions)

        assert first.partition(CONTEXT_SENTINEL)[0] == second.partition(CONTEXT_SENTINEL)[0]

    def test_clock_time_is_only_in_the_context(self) -> None:
        config = {
            "SYSTEM_PROMPT": "You are a helper.",
            "LLM_ENDPOINT_NAME": "test-model",
            "INJECT_TIME_USER": True,
        }
        body, _, context = cast("str", create_agent(config, tools=[]).instructions).partition(CONTEXT_SENTINEL)
        assert not re.search(r"\d{2}:\d{2}", body)
        assert re.search(r"\d{2}:\d{2}", context)

    def test_prompt_manuals_are_appended(self) -> None:
        config = {
            "SYSTEM_PROMPT": "Base.",
            "LLM_ENDPOINT_NAME": "test-model",
            "INJECT_TIME_USER": False,
        }
        agent = create_agent(config, tools=[], prompt_manuals="## Tool Manuals\nstuff")

        assert agent.instructions == "Base.\n\n## Tool Manuals\nstuff"

    def test_data_universe_is_appended_after_the_manuals(self) -> None:
        """Instructions first, then the reference data they describe."""
        config = {
            "SYSTEM_PROMPT": "Base.",
            "LLM_ENDPOINT_NAME": "test-model",
            "INJECT_TIME_USER": False,
        }
        agent = create_agent(
            config,
            tools=[],
            prompt_manuals="## Tool Manuals",
            data_universe="## Data Universe",
        )

        assert agent.instructions == "Base.\n\n## Tool Manuals\n\n## Data Universe"

    def test_empty_data_universe_adds_nothing(self) -> None:
        config = {
            "SYSTEM_PROMPT": "Base.",
            "LLM_ENDPOINT_NAME": "test-model",
            "INJECT_TIME_USER": False,
        }
        assert create_agent(config, tools=[], data_universe="").instructions == "Base."


class TestResolveIdentityPreamble:
    def test_returns_empty_when_disabled(self) -> None:
        assert resolve_identity_preamble({"INJECT_TIME_USER": False}) == ""

    def test_uses_the_resolved_username(self) -> None:
        preamble = resolve_identity_preamble({"INJECT_TIME_USER": True, "_resolved_username": "dave@example.com"})
        assert preamble.startswith("[Context:")
        assert "User: dave@example.com" in preamble


@patch("payloads.agent.runtime.request_context.get_user_workspace_client", return_value=None)
class TestResolveToolsClient:
    """Tools and workspace MCP servers never run as the app's service principal."""

    def test_user_token_wins(self, mock_user: MagicMock) -> None:
        mock_user.return_value = user_ws = MagicMock()

        assert resolve_tools_client(MagicMock()) is user_ws

    def test_missing_token_on_apps_fails(self, _mock_user: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DATABRICKS_APP_NAME", "test-app")

        with pytest.raises(UserTokenRequiredError, match="user_api_scopes"):
            resolve_tools_client(MagicMock())

    def test_missing_token_locally_uses_profile_client(
        self, _mock_user: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("DATABRICKS_APP_NAME", raising=False)
        local_ws = MagicMock()

        assert resolve_tools_client(local_ws) is local_ws

    def test_factories_without_tools_need_no_token(
        self, _mock_user: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DATABRICKS_APP_NAME", "test-app")
        config: dict[str, Any] = {"TOOLS": [], "SKILL_TOOLS": [{"type": "genie"}]}

        assert create_tools(MagicMock(), config, {}) == []
        assert create_python_tools(MagicMock(), config) == []

    @patch("payloads.agent.runtime.brick_mcp_servers._load_brick_spec_status")
    @patch("payloads.agent.runtime.brick_mcp_servers.resolve_instance_dir")
    def test_workspace_mcp_without_token_on_apps(
        self,
        mock_resolve: MagicMock,
        mock_load: MagicMock,
        _mock_user: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("DATABRICKS_APP_NAME", "test-app")
        mock_resolve.return_value = tmp_path
        _clear_brick_specs_cache()

        mock_load.return_value = _BrickSpecLoad()
        assert build_mcp_servers({}, MagicMock()) == []

        _clear_brick_specs_cache()
        mock_load.return_value = _BrickSpecLoad(specs=(cast(Any, MagicMock()),))
        with pytest.raises(UserTokenRequiredError):
            build_mcp_servers({}, MagicMock())
        _clear_brick_specs_cache()


class TestModelSettingsFor:
    """``spec.runtime.reasoning`` / ``model_settings`` -> ``ModelSettings`` (#473)."""

    def test_unset_leaves_the_request_unchanged(self) -> None:
        from payloads.agent.runtime.config import load_agent_config
        from payloads.agent.runtime.core import model_settings_for

        settings = model_settings_for(load_agent_config(None))
        assert settings.include_usage is True
        assert settings.extra_body is None
        assert settings.reasoning is None

    def test_thinking_and_effort_travel_in_extra_body(self) -> None:
        from payloads.agent.runtime.core import model_settings_for

        settings = model_settings_for({"REASONING_THINKING": "adaptive", "REASONING_EFFORT": "max"})
        assert settings.extra_body == {
            "thinking": {"type": "adaptive", "display": "summarized"},
            "reasoning_effort": "max",
        }

    def test_budget_is_sent_with_enabled_thinking(self) -> None:
        from payloads.agent.runtime.core import model_settings_for

        settings = model_settings_for({"REASONING_THINKING": "enabled", "REASONING_BUDGET_TOKENS": 4096})
        assert settings.extra_body == {
            "thinking": {"type": "enabled", "budget_tokens": 4096, "display": "summarized"},
        }

    def test_escape_hatch_is_merged(self) -> None:
        from payloads.agent.runtime.core import model_settings_for

        config: dict[str, Any] = {"REASONING_THINKING": "adaptive", "LLM_EXTRA_BODY": {"max_tokens": 16000}}
        settings = model_settings_for(config)
        assert settings.extra_body == {
            "max_tokens": 16000,
            "thinking": {"type": "adaptive", "display": "summarized"},
        }
        # The authored dict is not mutated by the merge.
        assert config["LLM_EXTRA_BODY"] == {"max_tokens": 16000}

    def test_create_agent_uses_the_settings(self) -> None:
        config = {
            "SYSTEM_PROMPT": "You are a helper.",
            "LLM_ENDPOINT_NAME": "databricks-claude-sonnet-5-5",
            "INJECT_TIME_USER": False,
            "REASONING_THINKING": "adaptive",
        }
        settings = create_agent(config, tools=[]).model_settings
        assert settings.extra_body == {"thinking": {"type": "adaptive", "display": "summarized"}}
        assert settings.include_usage is True

    @pytest.mark.parametrize("emit", [None, True])
    def test_display_defaults_to_summarized_while_emitting(self, emit: bool | None) -> None:
        """Sonnet 5.5 returns the summary empty unless asked (#473 follow-up)."""
        from payloads.agent.runtime.core import model_settings_for

        settings = model_settings_for({"REASONING_THINKING": "adaptive", "REASONING_EMIT": emit})
        assert settings.extra_body == {"thinking": {"type": "adaptive", "display": "summarized"}}

    def test_no_display_default_when_not_emitting(self) -> None:
        from payloads.agent.runtime.core import model_settings_for

        settings = model_settings_for({"REASONING_THINKING": "adaptive", "REASONING_EMIT": False})
        assert settings.extra_body == {"thinking": {"type": "adaptive"}}

    @pytest.mark.parametrize("emit", [True, False])
    def test_authored_display_wins(self, emit: bool) -> None:
        from payloads.agent.runtime.core import model_settings_for

        omitted = model_settings_for(
            {"REASONING_THINKING": "adaptive", "REASONING_DISPLAY": "omitted", "REASONING_EMIT": emit}
        )
        assert omitted.extra_body == {"thinking": {"type": "adaptive", "display": "omitted"}}
        summarized = model_settings_for(
            {"REASONING_THINKING": "enabled", "REASONING_DISPLAY": "summarized", "REASONING_EMIT": emit}
        )
        assert summarized.extra_body == {"thinking": {"type": "enabled", "display": "summarized"}}

    @pytest.mark.parametrize("thinking", ["between_tools", "disabled"])
    def test_no_display_for_modes_that_reject_it(self, thinking: str) -> None:
        """``between_tools`` 400s on ``display``; ``disabled`` has nothing to show."""
        from payloads.agent.runtime.core import model_settings_for

        settings = model_settings_for({"REASONING_THINKING": thinking})
        assert settings.extra_body == {"thinking": {"type": thinking}}
