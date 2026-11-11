"""Tests for the agent payload runtime config view + spec bridge.

``payloads.agent.config`` is the agent payload's typed view of the
opaque ``spec`` the framework hands it.  Covers the framework-default
fallbacks, the spec -> :class:`AgentConfig` kwargs bridge, and the
on-disk ``payload.yml`` loader.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from payloads.agent.config import (
    AgentConfig,
    agent_config_kwargs_from_spec,
    get_a2a_config,
    load_agent_config_model,
    load_agent_spec,
    resolve_agent_spec,
)
from payloads.agent.contracts import (
    AgentPayloadSpec,
    GatewayMcpRuntime,
    GatewaySkillsRuntime,
    ServingEndpointModel,
)


def _write_payload(instance_dir: Path, spec: dict[str, Any]) -> None:
    payload_dir = instance_dir / "payloads" / "agent"
    payload_dir.mkdir(parents=True)
    (payload_dir / "payload.yml").write_text(yaml.dump({"payload": {"adapter": "agent", "spec": spec}}))


def _endpoint(name: str) -> dict[str, str]:
    return {"kind": "serving_endpoint", "name": name}


class TestAgentConfigDefaults:
    def test_defaults(self) -> None:
        a = AgentConfig()
        assert a.llm_endpoint == "databricks-claude-sonnet-4-5"
        assert a.max_tool_calls_per_turn == 5
        assert a.max_iterations == 50
        assert a.inject_time_user is True
        assert a.mcp_only is True
        assert a.tools_typed == []
        assert a.a2a is None
        assert a.prompt_cache_enabled is True
        assert a.prompt_cache_static_ttl == "1h"
        # ``None`` selects the endpoint's own 5-minute default, which
        # keeps the ``ttl`` key out of the request payload entirely.
        assert a.prompt_cache_rolling_ttl is None
        assert a.tool_result_clearing_enabled is True
        assert a.tool_result_clearing_keep_tool_uses == 3
        # High, so ordinary tool-heavy turns keep the prompt-cache prefix
        # intact; occupancy below is the trigger that guarantees the valve
        # arms before the provider's window limit.
        assert a.tool_result_clearing_clear_at_least_chars == 300_000
        assert a.tool_result_clearing_clear_above_input_tokens == 150_000

    def test_is_frozen(self) -> None:
        a = AgentConfig()
        with pytest.raises(Exception):
            a.llm_endpoint = "changed"  # type: ignore[misc]


class TestGatewayKwargs:
    def test_defaults_are_local_sources(self) -> None:
        cfg = AgentConfig()
        assert cfg.llm_kind == "serving_endpoint"
        assert cfg.repository_skills is True
        assert cfg.gateway_mcp is None
        assert cfg.gateway_skills is None

    def test_model_service_maps_name_and_kind(self) -> None:
        spec = AgentPayloadSpec.model_validate(
            {"runtime": {"model": {"kind": "model_service", "name": "system.ai.claude-sonnet-4-5"}}}
        )
        kwargs = agent_config_kwargs_from_spec(spec)
        assert kwargs == {"llm_endpoint": "system.ai.claude-sonnet-4-5", "llm_kind": "model_service"}

    def test_gateway_blocks_flow_unchanged(self) -> None:
        mcp = {"gateway": {"pinned": [{"fqn": "system.ai.dbsql", "identity": "sp"}]}}
        spec = AgentPayloadSpec.model_validate({"runtime": {"tools": {"mcp": mcp}}})
        cfg = AgentConfig(**agent_config_kwargs_from_spec(spec))
        assert cfg.gateway_mcp == spec.runtime.tools.mcp.gateway
        assert cfg.repository_skills is True
        assert cfg.gateway_skills is None

    def test_omitted_gateway_block_defaults(self) -> None:
        spec = AgentPayloadSpec.model_validate({"runtime": {"tools": {"skills": {"source": "unity_gateway"}}}})
        cfg = AgentConfig(**agent_config_kwargs_from_spec(spec))
        assert cfg.gateway_skills == GatewaySkillsRuntime()

    def test_skills_user_catalog_next_to_repo_skills_reaches_runtime(self) -> None:
        skills = {"gateway": {"user_catalog": {"enabled": True, "locations": ["team.skills"]}}}
        spec = AgentPayloadSpec.model_validate({"runtime": {"tools": {"skills": skills}}})
        cfg = AgentConfig(**agent_config_kwargs_from_spec(spec))
        assert cfg.repository_skills is True
        assert cfg.gateway_skills == spec.runtime.tools.skills.gateway

    def test_prompt_cache_overrides_lazy_gateway_skills(self) -> None:
        runtime = {
            "tools": {"skills": {"source": "unity_gateway", "loading": "lazy"}},
            "context": {"prompt_cache": {"enabled": True}},
        }
        spec = AgentPayloadSpec.model_validate({"runtime": runtime})
        assert AgentConfig(**agent_config_kwargs_from_spec(spec)).skill_loading == "eager"

    def test_deprecated_gateway_source_without_block_still_mounts_gateway(self) -> None:
        spec = AgentPayloadSpec.model_validate({"runtime": {"tools": {"mcp": {"source": "unity_gateway"}}}})
        assert AgentConfig(**agent_config_kwargs_from_spec(spec)).gateway_mcp == GatewayMcpRuntime()


class TestKwargsFromSpec:
    def test_empty_spec_yields_no_overrides(self) -> None:
        assert agent_config_kwargs_from_spec(AgentPayloadSpec()) == {}

    def test_only_set_fields_emitted(self) -> None:
        spec = AgentPayloadSpec.model_validate({"runtime": {"model": _endpoint("ep"), "max_iterations": 7}})
        kwargs = agent_config_kwargs_from_spec(spec)
        assert kwargs == {"llm_endpoint": "ep", "llm_kind": "serving_endpoint", "max_iterations": 7}

    def test_tool_modes_and_definitions(self) -> None:
        spec = AgentPayloadSpec.model_validate(
            {
                "runtime": {
                    "tools": {
                        "mcp_only": False,
                        "skills": {"routing_header": "hi"},
                        "definitions": [
                            {
                                "name": "lookup",
                                "description": "d",
                                "type": "uc_function",
                                "function_name": "cat.sch.fn",
                            }
                        ],
                    }
                }
            }
        )
        kwargs = agent_config_kwargs_from_spec(spec)
        assert kwargs["mcp_only"] is False
        assert kwargs["skill_routing_header"] == "hi"
        assert len(kwargs["tools_typed"]) == 1

        cfg = AgentConfig(**kwargs)
        assert cfg.mcp_only is False
        assert cfg.tools_typed[0].name == "lookup"

    def test_python_tool_definition_maps(self) -> None:
        spec = AgentPayloadSpec.model_validate(
            {
                "runtime": {
                    "tools": {
                        "definitions": [
                            {
                                "name": "add_signal",
                                "description": "d",
                                "type": "python",
                                "callable_path": "my_pkg.mod.add_signal",
                            }
                        ],
                    }
                }
            }
        )
        cfg = AgentConfig(**agent_config_kwargs_from_spec(spec))
        assert cfg.tools_typed[0].type == "python"
        assert cfg.tools_typed[0].callable_path == "my_pkg.mod.add_signal"

    def test_finalize_and_reset_hooks_map(self) -> None:
        spec = AgentPayloadSpec.model_validate(
            {
                "runtime": {
                    "finalize": "my_pkg.hooks.finalize",
                    "reset": "my_pkg.hooks.reset",
                }
            }
        )
        kwargs = agent_config_kwargs_from_spec(spec)
        assert kwargs["finalize_callable"] == "my_pkg.hooks.finalize"
        assert kwargs["reset_callable"] == "my_pkg.hooks.reset"
        cfg = AgentConfig(**kwargs)
        assert cfg.finalize_callable == "my_pkg.hooks.finalize"
        assert cfg.reset_callable == "my_pkg.hooks.reset"

    def test_hooks_default_to_none(self) -> None:
        cfg = AgentConfig(**agent_config_kwargs_from_spec(AgentPayloadSpec()))
        assert cfg.finalize_callable is None
        assert cfg.reset_callable is None

    def test_prompt_cache_authored_values_win(self) -> None:
        spec = AgentPayloadSpec.model_validate(
            {
                "runtime": {
                    "context": {
                        "prompt_cache": {
                            "enabled": False,
                            "static_ttl": "5m",
                            "rolling_ttl": "1h",
                        }
                    }
                }
            }
        )
        cfg = AgentConfig(**agent_config_kwargs_from_spec(spec))

        assert cfg.prompt_cache_enabled is False
        assert cfg.prompt_cache_static_ttl == "5m"
        assert cfg.prompt_cache_rolling_ttl == "1h"

    def test_prompt_cache_unset_fields_inherit_defaults(self) -> None:
        spec = AgentPayloadSpec.model_validate({"runtime": {"context": {"prompt_cache": {"static_ttl": "5m"}}}})
        kwargs = agent_config_kwargs_from_spec(spec)
        assert kwargs == {"prompt_cache_static_ttl": "5m"}

        cfg = AgentConfig(**kwargs)
        assert cfg.prompt_cache_enabled is True
        assert cfg.prompt_cache_rolling_ttl is None

    def test_misspelled_ttl_is_rejected_at_load_time(self) -> None:
        """A bad TTL must fail the contract, not reach the gateway.

        ``"1hour"`` would be accepted by the endpoint as an unknown value
        and silently turn every request into a full-price cache write, so
        the closed ``Literal`` is the safety net.
        """
        with pytest.raises(ValidationError):
            AgentPayloadSpec.model_validate({"runtime": {"context": {"prompt_cache": {"static_ttl": "1hour"}}}})

    def test_unknown_context_key_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AgentPayloadSpec.model_validate({"runtime": {"context": {"promt_cache": {}}}})

    def test_data_universe_authored_values_win(self) -> None:
        spec = AgentPayloadSpec.model_validate(
            {
                "runtime": {
                    "context": {
                        "data_universe": {
                            "enabled": True,
                            "function": "cat.sch.get_landscape",
                            "heading": "## Catalog",
                            "max_rows": 50,
                            "timeout_seconds": 15,
                            "refresh_minutes": 30,
                        }
                    }
                }
            }
        )
        cfg = AgentConfig(**agent_config_kwargs_from_spec(spec))

        assert cfg.data_universe_enabled is True
        assert cfg.data_universe_function == "cat.sch.get_landscape"
        assert cfg.data_universe_heading == "## Catalog"
        assert cfg.data_universe_max_rows == 50
        assert cfg.data_universe_timeout_seconds == 15
        assert cfg.data_universe_refresh_minutes == 30

    def test_data_universe_defaults_to_off(self) -> None:
        """It needs a UC function to exist, so it cannot default to on."""
        cfg = AgentConfig()

        assert cfg.data_universe_enabled is False
        assert cfg.data_universe_function == "get_data_landscape"
        assert cfg.data_universe_refresh_minutes == 60

    def test_tool_result_clearing_authored_values_win(self) -> None:
        spec = AgentPayloadSpec.model_validate(
            {
                "runtime": {
                    "context": {
                        "tool_result_clearing": {
                            "enabled": False,
                            "keep_tool_uses": 5,
                            "clear_at_least_chars": 10000,
                            "clear_above_input_tokens": 60000,
                        }
                    }
                }
            }
        )
        cfg = AgentConfig(**agent_config_kwargs_from_spec(spec))

        assert cfg.tool_result_clearing_enabled is False
        assert cfg.tool_result_clearing_keep_tool_uses == 5
        assert cfg.tool_result_clearing_clear_at_least_chars == 10000
        assert cfg.tool_result_clearing_clear_above_input_tokens == 60000

    def test_tool_result_clearing_unset_fields_inherit_defaults(self) -> None:
        spec = AgentPayloadSpec.model_validate(
            {"runtime": {"context": {"tool_result_clearing": {"keep_tool_uses": 2}}}}
        )
        kwargs = agent_config_kwargs_from_spec(spec)
        assert kwargs == {"tool_result_clearing_keep_tool_uses": 2}
        cfg = AgentConfig(**kwargs)
        assert cfg.tool_result_clearing_enabled is True
        assert cfg.tool_result_clearing_keep_tool_uses == 2

    def test_negative_tool_result_clearing_values_are_rejected(self) -> None:
        for field in ("clear_at_least_chars", "clear_above_input_tokens"):
            with pytest.raises(ValidationError):
                AgentPayloadSpec.model_validate({"runtime": {"context": {"tool_result_clearing": {field: -1}}}})

    def test_zero_clear_above_input_tokens_is_authorable(self) -> None:
        """``0`` turns the occupancy trigger off, and must survive the
        ``is not None`` guard in the kwargs mapping rather than being read as
        "unset" and silently replaced by the default.
        """
        spec = AgentPayloadSpec.model_validate(
            {"runtime": {"context": {"tool_result_clearing": {"clear_above_input_tokens": 0}}}}
        )
        cfg = AgentConfig(**agent_config_kwargs_from_spec(spec))
        assert cfg.tool_result_clearing_clear_above_input_tokens == 0

    def test_data_universe_zero_refresh_is_authorable(self) -> None:
        """``0`` pins the block for the process; it is a real choice."""
        spec = AgentPayloadSpec.model_validate(
            {"runtime": {"context": {"data_universe": {"enabled": True, "refresh_minutes": 0}}}}
        )
        kwargs = agent_config_kwargs_from_spec(spec)

        assert kwargs == {"data_universe_enabled": True, "data_universe_refresh_minutes": 0}
        assert AgentConfig(**kwargs).data_universe_refresh_minutes == 0

    def test_data_universe_other_fields_require_enabled(self) -> None:
        """The SQL read is opt-in; configuring it without the toggle is rejected."""
        with pytest.raises(ValidationError, match="enabled must be set explicitly"):
            AgentPayloadSpec.model_validate(
                {"runtime": {"context": {"data_universe": {"function": "get_data_landscape"}}}}
            )

    @pytest.mark.parametrize(
        "block",
        [
            {"max_rows": 0},
            {"timeout_seconds": 0},
            {"refresh_minutes": -1},
            {"heding": "typo"},
        ],
    )
    def test_nonsensical_data_universe_values_are_rejected(self, block: dict[str, object]) -> None:
        with pytest.raises(ValidationError):
            AgentPayloadSpec.model_validate({"runtime": {"context": {"data_universe": block}}})

    def test_a2a_card_carried_into_config(self) -> None:
        spec = AgentPayloadSpec.model_validate(
            {
                "integrations": {
                    "a2a": {
                        "enabled": True,
                        "card": {
                            "name": "My Agent",
                            "description": "d",
                            "version": "1.0.0",
                            "skills": [
                                {
                                    "id": "ask",
                                    "name": "Ask",
                                    "description": "qa",
                                    "tags": ["general"],
                                }
                            ],
                        },
                    }
                }
            }
        )
        cfg = AgentConfig(**agent_config_kwargs_from_spec(spec))
        a2a = get_a2a_config(cfg)
        assert a2a is not None
        assert a2a.card is not None
        assert a2a.card.name == "My Agent"


class TestLoadFromDisk:
    def test_load_agent_spec_missing_file_returns_defaults(self, tmp_path: Path) -> None:
        spec = load_agent_spec(tmp_path)
        assert spec == AgentPayloadSpec()

    def test_load_agent_spec_reads_payload_yml(self, tmp_path: Path) -> None:
        _write_payload(tmp_path, {"runtime": {"model": _endpoint("ep")}})
        spec = load_agent_spec(tmp_path)
        assert spec.runtime.model == ServingEndpointModel(kind="serving_endpoint", name="ep")

    def test_load_agent_config_model_applies_defaults(self, tmp_path: Path) -> None:
        _write_payload(tmp_path, {"runtime": {"max_iterations": 3}})
        cfg = load_agent_config_model(tmp_path)
        assert cfg.max_iterations == 3
        # unset field falls back to the framework default
        assert cfg.llm_endpoint == "databricks-claude-sonnet-4-5"

    def test_malformed_spec_raises(self, tmp_path: Path) -> None:
        _write_payload(tmp_path, {"runtime": {"unknown_key": 1}})
        with pytest.raises(Exception):
            load_agent_spec(tmp_path)


class TestResolveAgentSpec:
    def test_prefers_instance_dir(self, tmp_path: Path) -> None:
        _write_payload(tmp_path, {"runtime": {"model": _endpoint("from-disk")}})
        spec = resolve_agent_spec(tmp_path, {"runtime": {"model": _endpoint("from-raw")}})
        assert spec.runtime.model == ServingEndpointModel(kind="serving_endpoint", name="from-disk")

    def test_falls_back_to_raw_spec(self) -> None:
        spec = resolve_agent_spec(None, {"runtime": {"model": _endpoint("from-raw")}})
        assert spec.runtime.model == ServingEndpointModel(kind="serving_endpoint", name="from-raw")

    def test_none_raw_spec_yields_defaults(self) -> None:
        assert resolve_agent_spec(None, None) == AgentPayloadSpec()


class TestReasoningSpec:
    """``spec.runtime.reasoning`` / ``model_settings`` -> AgentConfig (#473)."""

    def test_unset_sends_nothing(self) -> None:
        cfg = AgentConfig(**agent_config_kwargs_from_spec(AgentPayloadSpec()))
        assert cfg.reasoning_thinking is None
        assert cfg.reasoning_budget_tokens is None
        assert cfg.reasoning_effort is None
        assert cfg.reasoning_emit is True
        assert cfg.llm_extra_body is None

    def test_authored_values_win(self) -> None:
        spec = AgentPayloadSpec.model_validate(
            {
                "runtime": {
                    "reasoning": {"thinking": "enabled", "budget_tokens": 4096, "effort": "max", "emit": False},
                    "model_settings": {"extra_body": {"max_tokens": 16000}},
                }
            }
        )
        cfg = AgentConfig(**agent_config_kwargs_from_spec(spec))
        assert cfg.reasoning_thinking == "enabled"
        assert cfg.reasoning_budget_tokens == 4096
        assert cfg.reasoning_effort == "max"
        assert cfg.reasoning_emit is False
        assert cfg.llm_extra_body == {"max_tokens": 16000}

    def test_budget_requires_enabled_thinking(self) -> None:
        with pytest.raises(ValidationError, match="budget_tokens requires thinking: enabled"):
            AgentPayloadSpec.model_validate({"runtime": {"reasoning": {"thinking": "adaptive", "budget_tokens": 4096}}})

    def test_budget_has_a_floor(self) -> None:
        with pytest.raises(ValidationError):
            AgentPayloadSpec.model_validate({"runtime": {"reasoning": {"thinking": "enabled", "budget_tokens": 10}}})

    @pytest.mark.parametrize("block", [{"thinking": "on"}, {"effort": "extreme"}, {"emitt": True}])
    def test_unknown_values_and_keys_are_rejected(self, block: dict[str, object]) -> None:
        with pytest.raises(ValidationError):
            AgentPayloadSpec.model_validate({"runtime": {"reasoning": block}})

    @pytest.mark.parametrize(
        ("key", "owner"),
        [
            ("thinking", "spec.runtime.reasoning.thinking"),
            ("reasoning_effort", "spec.runtime.reasoning.effort"),
            ("model", "spec.runtime.model"),
            ("messages", "the Agents SDK"),
            ("stream", "the Agents SDK"),
        ],
    )
    def test_extra_body_rejects_keys_owned_elsewhere(self, key: str, owner: str) -> None:
        with pytest.raises(ValidationError, match=owner):
            AgentPayloadSpec.model_validate({"runtime": {"model_settings": {"extra_body": {key: "x"}}}})


class TestReasoningDisplaySpec:
    """``spec.runtime.reasoning.display`` (#473 follow-up)."""

    @pytest.mark.parametrize("thinking", ["adaptive", "enabled"])
    @pytest.mark.parametrize("display", ["summarized", "omitted"])
    def test_accepted_with_thinking_that_displays(self, thinking: str, display: str) -> None:
        reasoning: dict[str, object] = {"thinking": thinking, "display": display}
        if thinking == "enabled":
            reasoning["budget_tokens"] = 2048
        spec = AgentPayloadSpec.model_validate({"runtime": {"reasoning": reasoning}})
        assert AgentConfig(**agent_config_kwargs_from_spec(spec)).reasoning_display == display

    def test_unset_maps_to_none(self) -> None:
        spec = AgentPayloadSpec.model_validate({"runtime": {"reasoning": {"thinking": "adaptive"}}})
        assert AgentConfig(**agent_config_kwargs_from_spec(spec)).reasoning_display is None

    def test_requires_thinking(self) -> None:
        """The endpoint 400s on a thinking block without ``type``."""
        with pytest.raises(ValidationError, match="display requires thinking to be set"):
            AgentPayloadSpec.model_validate({"runtime": {"reasoning": {"display": "summarized"}}})

    @pytest.mark.parametrize("thinking", ["between_tools", "disabled"])
    def test_rejected_where_nothing_can_be_displayed(self, thinking: str) -> None:
        with pytest.raises(ValidationError, match=f"display is not allowed with thinking: {thinking}"):
            AgentPayloadSpec.model_validate({"runtime": {"reasoning": {"thinking": thinking, "display": "summarized"}}})

    def test_unknown_value_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AgentPayloadSpec.model_validate({"runtime": {"reasoning": {"thinking": "adaptive", "display": "full"}}})


class TestReasoningBudgetSpec:
    """``thinking: enabled`` and its budget (#473 review)."""

    def test_enabled_requires_a_budget(self) -> None:
        with pytest.raises(ValidationError, match="thinking: enabled requires budget_tokens"):
            AgentPayloadSpec.model_validate({"runtime": {"reasoning": {"thinking": "enabled"}}})

    def test_budget_must_leave_room_below_max_tokens(self) -> None:
        with pytest.raises(ValidationError, match="must be smaller than"):
            AgentPayloadSpec.model_validate(
                {
                    "runtime": {
                        "reasoning": {"thinking": "enabled", "budget_tokens": 8000},
                        "model_settings": {"extra_body": {"max_tokens": 8000}},
                    }
                }
            )

    @pytest.mark.parametrize("max_tokens", ["16000", 16000.0, True])
    def test_budget_needs_an_integer_max_tokens(self, max_tokens: Any) -> None:
        with pytest.raises(ValidationError, match="max_tokens must be an integer"):
            AgentPayloadSpec.model_validate(
                {
                    "runtime": {
                        "reasoning": {"thinking": "enabled", "budget_tokens": 4096},
                        "model_settings": {"extra_body": {"max_tokens": max_tokens}},
                    }
                }
            )

    def test_budget_below_max_tokens_is_accepted(self) -> None:
        spec = AgentPayloadSpec.model_validate(
            {
                "runtime": {
                    "reasoning": {"thinking": "enabled", "budget_tokens": 4096},
                    "model_settings": {"extra_body": {"max_tokens": 8000}},
                }
            }
        )
        assert spec.runtime.reasoning.budget_tokens == 4096

    def test_budget_without_max_tokens_is_left_to_the_endpoint(self) -> None:
        spec = AgentPayloadSpec.model_validate(
            {"runtime": {"reasoning": {"thinking": "enabled", "budget_tokens": 4096}}}
        )
        assert spec.runtime.model_settings.extra_body is None


class TestClaude5ThinkingSpec:
    """Claude 5.x rejects ``thinking.type.enabled`` (live 400 on Sonnet 5.5)."""

    @pytest.mark.parametrize(
        "model",
        [
            {"kind": "serving_endpoint", "name": "databricks-claude-sonnet-5-5"},
            {"kind": "model_service", "name": "system.ai.claude-sonnet-5-5"},
            {"kind": "serving_endpoint", "name": "databricks-claude-haiku-5-5"},
            {"kind": "serving_endpoint", "name": "databricks-claude-opus-5-5"},
        ],
    )
    @pytest.mark.parametrize("thinking", ["enabled", "disabled"])
    def test_rejects_enabled_and_disabled(self, model: dict[str, str], thinking: str) -> None:
        reasoning: dict[str, object] = {"thinking": thinking}
        if thinking == "enabled":
            reasoning["budget_tokens"] = 4096
        with pytest.raises(ValidationError, match="not supported on Claude 5.x"):
            AgentPayloadSpec.model_validate({"runtime": {"model": model, "reasoning": reasoning}})

    def test_adaptive_and_effort_are_the_supported_path(self) -> None:
        spec = AgentPayloadSpec.model_validate(
            {
                "runtime": {
                    "model": {"kind": "serving_endpoint", "name": "databricks-claude-sonnet-5-5"},
                    "reasoning": {"thinking": "adaptive", "effort": "high", "display": "omitted"},
                    "model_settings": {"extra_body": {"max_tokens": 16000}},
                }
            }
        )
        assert spec.runtime.reasoning.thinking == "adaptive"
        assert spec.runtime.reasoning.effort == "high"

    def test_sonnet_4_5_still_allows_enabled(self) -> None:
        spec = AgentPayloadSpec.model_validate(
            {
                "runtime": {
                    "model": {"kind": "serving_endpoint", "name": "databricks-claude-sonnet-4-5"},
                    "reasoning": {"thinking": "enabled", "budget_tokens": 4096},
                }
            }
        )
        assert spec.runtime.reasoning.thinking == "enabled"


def _skills_gateway(spec: AgentPayloadSpec) -> GatewaySkillsRuntime:
    gateway = spec.runtime.tools.skills.gateway
    assert gateway is not None
    return gateway


class TestGatewayFqnVariables:
    _SPEC: dict[str, Any] = {
        "system_prompt": "keep ${var.catalog} literal",
        "tools": {
            "mcp": {
                "source": "unity_gateway",
                "gateway": {"pinned": [{"fqn": "${var.catalog}.tools.dbsql", "identity": "sp"}]},
            },
            "skills": {
                "source": "unity_gateway",
                "gateway": {
                    "pinned": [{"fqn": "${var.catalog}.${var.schema}.triage", "identity": "sp"}],
                    "discovery": {"enabled": True, "locations": ["${var.catalog}.shared"]},
                },
            },
        },
    }

    def test_placeholders_validate_unresolved(self, tmp_path: Path) -> None:
        _write_payload(tmp_path, {"runtime": self._SPEC})
        spec = load_agent_spec(tmp_path)
        assert _skills_gateway(spec).pinned[0].fqn == "${var.catalog}.${var.schema}.triage"

    def test_variables_resolve_only_gateway_blocks(self, tmp_path: Path) -> None:
        _write_payload(tmp_path, {"runtime": self._SPEC})
        spec = load_agent_spec(tmp_path, variables={"catalog": "main", "schema": "skills"})
        mcp_gateway = spec.runtime.tools.mcp.gateway
        assert mcp_gateway is not None
        assert mcp_gateway.pinned[0].fqn == "main.tools.dbsql"
        assert _skills_gateway(spec).pinned[0].fqn == "main.skills.triage"
        assert _skills_gateway(spec).discovery.locations == ["main.shared"]
        assert spec.runtime.system_prompt == "keep ${var.catalog} literal"

    def test_undefined_variable_fails_loudly(self, tmp_path: Path) -> None:
        _write_payload(tmp_path, {"runtime": self._SPEC})
        with pytest.raises(ValueError, match=r"\$\{var\.schema\}"):
            load_agent_spec(tmp_path, variables={"catalog": "main"})

    def test_raw_spec_path_resolves_too(self) -> None:
        spec = resolve_agent_spec(None, {"runtime": self._SPEC}, variables={"catalog": "c", "schema": "s"})
        assert _skills_gateway(spec).pinned[0].fqn == "c.s.triage"

    @pytest.mark.parametrize("fqn", ["${var.catalog}..triage", "${catalog}.s.triage", "-x.s.triage"])
    def test_malformed_templates_are_rejected(self, fqn: str) -> None:
        with pytest.raises(ValidationError):
            AgentPayloadSpec.model_validate(
                {"runtime": {"tools": {"skills": {"gateway": {"pinned": [{"fqn": fqn, "identity": "sp"}]}}}}}
            )

    def test_model_service_name_stays_strict(self) -> None:
        with pytest.raises(ValidationError):
            AgentPayloadSpec.model_validate({"runtime": {"model": {"kind": "model_service", "name": "${var.c}.s.m"}}})
