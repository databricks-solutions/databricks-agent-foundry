"""Agent payload runtime config view + spec bridge (payload-owned).

The framework hands the agent payload an opaque ``spec`` mapping (the
``payload.spec`` block of ``payloads/agent/payload.yml``).  This module
is the agent payload's typed view of that spec:

* :func:`load_agent_spec` reads + validates the spec into the typed
  :class:`payloads.agent.contracts.AgentPayloadSpec`.
* :class:`AgentConfig` is the runtime view of ``spec.runtime`` -- a
  frozen model with framework-level defaults for every field.
* :func:`agent_config_kwargs_from_spec` maps a validated spec into
  :class:`AgentConfig` kwargs (only author-set fields, so unset keys
  fall back to the defaults).
* :func:`get_a2a_config` is the typed accessor the A2A adapter uses to
  read the authored agent card.

Nothing in :mod:`agent_foundry` (the framework) imports this module --
agent specifics live entirely in the payload.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from agent_foundry._instance_layout import DEFAULT_PAYLOAD
from agent_foundry._payload_spec import read_spec, spec_section
from agent_foundry.contracts.a2a import A2AConfig
from agent_foundry.contracts.tools import Tool
from payloads.agent.contracts import (
    DEFAULT_CLEAR_ABOVE_INPUT_TOKENS,
    DEFAULT_SKILL_ROUTING_HEADER,
    AgentMemoryRuntime,
    AgentPayloadSpec,
    AgentPlanRuntime,
    AgentToolsRuntime,
    GatewayMcpRuntime,
    GatewaySkillsRuntime,
    UserSkills,
    effective_skill_loading,
)


class AgentConfig(BaseModel, frozen=True):
    """Runtime view of the agent's ``spec.runtime`` from ``payload.yml``.

    Populated from :class:`payloads.agent.contracts.AgentPayloadSpec` by
    :func:`agent_config_kwargs_from_spec`; every field carries a
    framework default so an omitted ``spec.runtime`` key falls back here.
    """

    # The model name for either kind: a serving endpoint name, or a
    # Unity Gateway model service FQN.  From ``spec.runtime.model``.
    llm_endpoint: str = "databricks-claude-sonnet-4-5"
    llm_kind: Literal["serving_endpoint", "model_service"] = "serving_endpoint"
    system_prompt: str = (
        "You are a helpful assistant with access to tools. "
        "Use the available tools to answer the user's question. "
        "If a tool returns an error, explain what happened clearly."
    )
    max_tool_calls_per_turn: int = 5
    max_iterations: int = 50
    inject_time_user: bool = True

    # When true (the default), the runtime suppresses the direct tool
    # factories and attaches tools exclusively via MCP servers.  Authored
    # via ``spec.runtime.tools.mcp_only``; set ``false`` to keep direct
    # Genie / Vector Search / UC Function tools.
    mcp_only: bool = True

    # Tool sources, from ``spec.runtime.tools.mcp`` / ``.skills``.
    # ``gateway_mcp`` is ``None`` without a ``mcp.gateway`` block; it sits
    # next to the ``server.yml`` servers.  ``gateway_skills`` is ``None``
    # without Unity Catalog skills (``skills.unity_catalog`` or a Unity
    # Catalog ``user_skills`` store); it sits next to repository skills.
    repository_skills: bool = True
    gateway_mcp: GatewayMcpRuntime | None = None
    gateway_skills: GatewaySkillsRuntime | None = None
    # ``spec.runtime.tools.skills.user_skills``; ``None`` unless the Lakebase store is on.
    user_skills: UserSkills | None = None
    # ``spec.runtime.context.memory``; ``None`` while memory is off.
    memory: AgentMemoryRuntime | None = None
    # ``spec.runtime.context.plan``; ``None`` while the plan is off.
    plan: AgentPlanRuntime | None = None

    # Prose injected above the lazy-mode skill routing index (issue #98),
    # from ``spec.runtime.tools.skills.routing_header``.  The skills
    # assembler is the actual consumer (the runtime reads the
    # pre-assembled prompt).
    skill_routing_header: str = DEFAULT_SKILL_ROUTING_HEADER
    # ``spec.runtime.tools.skills.loading``.  Foundry skills take the mode
    # from the assembled output; gateway skills read it from here.
    skill_loading: Literal["eager", "lazy"] = "eager"

    # Typed view of the agent's direct tool list, from
    # ``spec.runtime.tools.definitions``.
    tools_typed: list[Tool] = []

    # Provider prompt caching, from ``spec.runtime.context.prompt_cache``.
    # ``rolling_ttl`` defaults to ``None`` rather than ``"5m"`` because
    # omitting the key entirely selects the endpoint's own 5-minute
    # default, which keeps the request payload minimal.
    prompt_cache_enabled: bool = True
    prompt_cache_static_ttl: str | None = "1h"
    prompt_cache_rolling_ttl: str | None = None

    # Data-universe injection, from ``spec.runtime.context.data_universe``.
    # Off by default because it requires a UC table function to exist.
    data_universe_enabled: bool = False
    data_universe_function: str = "get_data_landscape"
    data_universe_heading: str = "## Data Universe"
    data_universe_max_rows: int = 200
    data_universe_timeout_seconds: int = 30
    data_universe_refresh_minutes: int = 60

    # Client-side tool-result clearing, from
    # ``spec.runtime.context.tool_result_clearing``.  On by default so
    # long conversations do not grow without bound.  These mirror the
    # defaults in ``payloads.agent.runtime.tool_result_clearing``, which
    # documents why occupancy is the primary trigger and savings-available
    # is set high; the values are repeated rather than imported because
    # this module also loads on the deploy path, without the Agents SDK.
    # ``tests/unit/runtime/test_tool_result_clearing.py`` asserts they agree.
    tool_result_clearing_enabled: bool = True
    tool_result_clearing_keep_tool_uses: int = 3
    tool_result_clearing_clear_at_least_chars: int = 300_000
    tool_result_clearing_clear_above_input_tokens: int = DEFAULT_CLEAR_ABOVE_INPUT_TOKENS

    # Thinking-model settings, from ``spec.runtime.reasoning``.  ``None``
    # sends nothing, leaving the endpoint's default in charge -- which is
    # what keeps non-thinking models' requests unchanged.
    reasoning_thinking: str | None = None
    reasoning_budget_tokens: int | None = None
    # Authored value only; ``core.model_settings_for`` applies the
    # ``summarized`` default while ``reasoning_emit`` is on.
    reasoning_display: str | None = None
    reasoning_effort: str | None = None
    # Whether ``/invocations`` streams reasoning (chat UI shows it).
    reasoning_emit: bool = True

    # Raw request-body settings, from ``spec.runtime.model_settings``.
    llm_extra_body: dict[str, Any] | None = None

    # Optional generic lifecycle hooks (dotted import paths), from
    # ``spec.runtime.finalize`` / ``spec.runtime.reset``.  ``finalize``
    # returns a dict merged into ``ResponsesAgentResponse.custom_outputs``
    # after a run; ``reset`` runs at request start to clear per-request
    # accumulation.  ``None`` when unset (the common case).
    finalize_callable: str | None = None
    reset_callable: str | None = None

    # Optional A2A agent-card configuration, from
    # ``spec.integrations.a2a.card``.  ``None`` means no card was
    # authored; the A2A adapter falls back to its built-in default card.
    a2a: A2AConfig | None = None


def get_a2a_config(agent_config: AgentConfig) -> A2AConfig | None:
    """Return the typed A2A card config for an agent, if any.

    Thin accessor so the A2A adapter does not reach into
    :class:`AgentConfig` directly.  Returns the stored ``A2AConfig``
    instance -- treat it as read-only.
    """
    return agent_config.a2a


_SpecBlocks = tuple[tuple[tuple[str, ...], tuple[str, ...]], ...]

_GATEWAY_BLOCKS: _SpecBlocks = (
    (("runtime", "tools", "mcp"), ("gateway",)),
    (("runtime", "tools", "skills"), ("gateway", "unity_catalog", "user_skills")),
)
# Names another brick's deploy outputs; unknown until that brick has run.
_BRICK_OUTPUT_BLOCKS: _SpecBlocks = ((("ui", "speech_input"), ("endpoint",)),)


def substitute_gateway_vars(raw_spec: dict[str, Any], variables: Mapping[str, str]) -> dict[str, Any]:
    """*raw_spec* with ``${var.*}`` resolved inside the Unity Catalog blocks.

    Only ``tools.mcp.gateway`` and the skills ``unity_catalog``,
    ``user_skills`` and deprecated ``gateway`` blocks are touched: their
    FQNs name deploy-specific catalogs and schemas.  An undefined
    variable raises ``ValueError`` rather than reaching a gateway URL.
    ``ui.speech_input.endpoint`` is resolved too, but an undefined
    variable stays as written: it is a brick output (e.g.
    ``serving_endpoint_name__speech``) and destroy or an early resolve
    runs without it.  The app deploy refuses a placeholder left there.
    """
    resolved = copy.deepcopy(raw_spec)
    _substitute_blocks(resolved, _GATEWAY_BLOCKS, variables, strict=True)
    _substitute_blocks(resolved, _BRICK_OUTPUT_BLOCKS, variables, strict=False)
    return resolved


def _substitute_blocks(
    spec: dict[str, Any], blocks: _SpecBlocks, variables: Mapping[str, str], *, strict: bool
) -> None:
    from agent_foundry.variables import substitute_obj

    for path, keys in blocks:
        block = spec_section(spec, *path)
        for key in keys:
            if key in block:
                block[key] = substitute_obj(block[key], dict(variables), strict=strict)


def load_agent_spec(
    instance_dir: Path,
    *,
    payload: str = DEFAULT_PAYLOAD,
    variables: Mapping[str, str] | None = None,
) -> AgentPayloadSpec:
    """Read and validate the agent payload's ``spec`` from ``payload.yml``.

    Returns a fully-defaulted :class:`AgentPayloadSpec` when the file (or
    the ``spec`` block) is absent, so callers always get a usable object.
    Raises ``pydantic.ValidationError`` on a malformed / misspelled spec.
    With *variables*, gateway FQN placeholders are resolved first (see
    :func:`substitute_gateway_vars`); without them they stay as authored.
    """
    raw = read_spec(instance_dir, payload=payload)
    if variables is not None:
        raw = substitute_gateway_vars(raw, variables)
    return AgentPayloadSpec.model_validate(raw)


def agent_config_kwargs_from_spec(spec: AgentPayloadSpec) -> dict[str, Any]:
    """Map a validated ``spec`` into runtime :class:`AgentConfig` kwargs.

    Only spec fields the author actually set are emitted, so unset keys
    fall back to the :class:`AgentConfig` framework defaults.  The A2A
    *card* (``spec.integrations.a2a.card``) is carried here so the A2A
    adapter can read it; whether the A2A routes are mounted is a separate
    concern driven by ``spec.integrations.a2a.enabled``.
    """
    runtime = spec.runtime
    result: dict[str, Any] = {}
    if runtime.model is not None:
        result["llm_endpoint"] = runtime.model.name
        result["llm_kind"] = runtime.model.kind
    result.update(_tool_source_kwargs(runtime.tools))
    if runtime.system_prompt is not None:
        result["system_prompt"] = runtime.system_prompt
    if runtime.max_tool_calls_per_turn is not None:
        result["max_tool_calls_per_turn"] = runtime.max_tool_calls_per_turn
    if runtime.max_iterations is not None:
        result["max_iterations"] = runtime.max_iterations
    if runtime.inject_time_user is not None:
        result["inject_time_user"] = runtime.inject_time_user
    if runtime.tools.mcp_only is not None:
        result["mcp_only"] = runtime.tools.mcp_only
    if runtime.tools.skills.routing_header is not None:
        result["skill_routing_header"] = runtime.tools.skills.routing_header
    if runtime.tools.skills.loading is not None:
        result["skill_loading"] = effective_skill_loading(
            runtime.tools.skills.loading, prompt_cache_enabled=bool(runtime.context.prompt_cache.enabled)
        )
    user_skills = runtime.tools.skills.lakebase_user_skills()
    if user_skills is not None:
        result["user_skills"] = user_skills
    if runtime.tools.definitions is not None:
        result["tools_typed"] = list(runtime.tools.definitions)
    if runtime.finalize is not None:
        result["finalize_callable"] = runtime.finalize
    if runtime.reset is not None:
        result["reset_callable"] = runtime.reset

    if spec.memory_enabled():
        result["memory"] = runtime.context.memory
    if spec.plan_enabled():
        result["plan"] = runtime.context.plan

    prompt_cache = runtime.context.prompt_cache
    if prompt_cache.enabled is not None:
        result["prompt_cache_enabled"] = prompt_cache.enabled
    if prompt_cache.static_ttl is not None:
        result["prompt_cache_static_ttl"] = prompt_cache.static_ttl
    if prompt_cache.rolling_ttl is not None:
        result["prompt_cache_rolling_ttl"] = prompt_cache.rolling_ttl

    universe = runtime.context.data_universe
    for field, value in (
        ("data_universe_enabled", universe.enabled),
        ("data_universe_function", universe.function),
        ("data_universe_heading", universe.heading),
        ("data_universe_max_rows", universe.max_rows),
        ("data_universe_timeout_seconds", universe.timeout_seconds),
        ("data_universe_refresh_minutes", universe.refresh_minutes),
    ):
        if value is not None:
            result[field] = value

    clearing = runtime.context.tool_result_clearing
    if clearing.enabled is not None:
        result["tool_result_clearing_enabled"] = clearing.enabled
    if clearing.keep_tool_uses is not None:
        result["tool_result_clearing_keep_tool_uses"] = clearing.keep_tool_uses
    if clearing.clear_at_least_chars is not None:
        result["tool_result_clearing_clear_at_least_chars"] = clearing.clear_at_least_chars
    if clearing.clear_above_input_tokens is not None:
        result["tool_result_clearing_clear_above_input_tokens"] = clearing.clear_above_input_tokens

    reasoning = runtime.reasoning
    for field, value in (
        ("reasoning_thinking", reasoning.thinking),
        ("reasoning_budget_tokens", reasoning.budget_tokens),
        ("reasoning_display", reasoning.display),
        ("reasoning_effort", reasoning.effort),
        ("reasoning_emit", reasoning.emit),
    ):
        if value is not None:
            result[field] = value
    if runtime.model_settings.extra_body is not None:
        result["llm_extra_body"] = dict(runtime.model_settings.extra_body)

    a2a = spec.integrations.a2a
    if a2a.card is not None:
        result["a2a"] = A2AConfig(card=a2a.card, enable_v0_3_compat=a2a.enable_v0_3_compat)
    return result


def _tool_source_kwargs(tools: AgentToolsRuntime) -> dict[str, Any]:
    """Map the MCP and skills gateway blocks and the repository skills switch."""
    result: dict[str, Any] = {}
    gateway_mcp = tools.mcp.effective_gateway()
    if gateway_mcp is not None:
        result["gateway_mcp"] = gateway_mcp
    if not tools.skills.repository_enabled():
        result["repository_skills"] = False
    gateway_skills = tools.skills.effective_gateway()
    if gateway_skills is not None:
        result["gateway_skills"] = gateway_skills
    return result


def load_agent_config_model(
    instance_dir: Path,
    *,
    payload: str = DEFAULT_PAYLOAD,
    variables: Mapping[str, str] | None = None,
) -> AgentConfig:
    """Convenience: read the spec and build the runtime :class:`AgentConfig`."""
    spec = load_agent_spec(instance_dir, payload=payload, variables=variables)
    return AgentConfig(**agent_config_kwargs_from_spec(spec))


def resolve_agent_spec(
    instance_dir: Path | None,
    raw_spec: Any | None = None,
    *,
    payload: str = DEFAULT_PAYLOAD,
    variables: Mapping[str, str] | None = None,
) -> AgentPayloadSpec:
    """Resolve the typed ``spec``, preferring on-disk ``payload.yml``.

    When *instance_dir* is given, reads + validates the file (the source
    of truth).  Otherwise validates the *raw_spec* mapping the framework
    passed through the payload-adapter seam (used by dry-run / hermetic
    paths that have no instance directory).  *variables* resolves gateway
    FQN placeholders, as in :func:`load_agent_spec`.
    """
    if instance_dir is not None:
        return load_agent_spec(instance_dir, payload=payload, variables=variables)
    raw = dict(raw_spec or {})
    if variables is not None:
        raw = substitute_gateway_vars(raw, variables)
    return AgentPayloadSpec.model_validate(raw)


__all__ = [
    "AgentConfig",
    "agent_config_kwargs_from_spec",
    "get_a2a_config",
    "load_agent_config_model",
    "load_agent_spec",
    "resolve_agent_spec",
]
