"""
Agent Configuration
====================
Loads and validates agent settings using the Pydantic ``AgentConfig``
model from ``payloads.agent.config``.  Per-instance config comes from
the agent payload's ``spec`` in ``payloads/agent/payload.yml`` (the
single config contract), read at runtime via ``load_agent_config_model``.

Returns a ``dict[str, Any]`` for backward compatibility with the runtime
code in ``agent.py`` that adds runtime-injected keys (``PROMPT_NAME``,
``SKILL_TOOLS``, ``DEPLOYER_USER``, etc.).

Tracing keys (``TRACING_ENABLED``, ``TRACING_EXPERIMENT``, ...) used to
be surfaced here but were never honoured at runtime: tracing flows
unconditionally via ``setup_tracing_early()`` driven by ``app.yaml``
env vars.  See ``bricks/agent/tracing.py`` for the live path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_foundry._instance_layout import resolve_payload_subdir
from payloads.agent.config import AgentConfig, load_agent_config_model


def load_agent_config(
    instance_dir: Path | None,
    variables: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Return a merged config dict (defaults + instance overrides).

    Validates via Pydantic ``AgentConfig`` internally, then returns a
    ``dict[str, Any]`` with uppercase keys for backward compatibility.
    """
    agent_cfg = AgentConfig()
    tools: list[dict[str, Any]] = []
    skills_result: dict[str, Any] | None = None

    if instance_dir is not None:
        # Single source: the agent payload's ``spec`` in payload.yml.
        agent_cfg = load_agent_config_model(instance_dir, variables=variables)
        tools = [t.model_dump(mode="json") for t in agent_cfg.tools_typed]
        skills_result = _assemble_skills_if_present(instance_dir, variables)

    config: dict[str, Any] = {
        "LLM_ENDPOINT_NAME": agent_cfg.llm_endpoint,
        "LLM_KIND": agent_cfg.llm_kind,
        "SYSTEM_PROMPT": agent_cfg.system_prompt,
        "MAX_TOOL_CALLS_PER_TURN": agent_cfg.max_tool_calls_per_turn,
        "MAX_ITERATIONS": agent_cfg.max_iterations,
        "INJECT_TIME_USER": agent_cfg.inject_time_user,
        "MCP_ONLY": agent_cfg.mcp_only,
        # Tool sources (``spec.runtime.tools.mcp`` / ``.skills``); the
        # gateway blocks stay typed models and are ``None`` when absent.
        "REPOSITORY_SKILLS": agent_cfg.repository_skills,
        "GATEWAY_MCP": agent_cfg.gateway_mcp,
        "GATEWAY_SKILLS": agent_cfg.gateway_skills,
        "USER_SKILLS": agent_cfg.user_skills,
        "MEMORY": agent_cfg.memory,
        "PLAN": agent_cfg.plan,
        "TOOLS": tools,
        "PROMPT_CACHE_ENABLED": agent_cfg.prompt_cache_enabled,
        "PROMPT_CACHE_STATIC_TTL": agent_cfg.prompt_cache_static_ttl,
        "PROMPT_CACHE_ROLLING_TTL": agent_cfg.prompt_cache_rolling_ttl,
        "DATA_UNIVERSE_ENABLED": agent_cfg.data_universe_enabled,
        "DATA_UNIVERSE_FUNCTION": agent_cfg.data_universe_function,
        "DATA_UNIVERSE_HEADING": agent_cfg.data_universe_heading,
        "DATA_UNIVERSE_MAX_ROWS": agent_cfg.data_universe_max_rows,
        "DATA_UNIVERSE_TIMEOUT_SECONDS": agent_cfg.data_universe_timeout_seconds,
        "DATA_UNIVERSE_REFRESH_MINUTES": agent_cfg.data_universe_refresh_minutes,
        "TOOL_RESULT_CLEARING_ENABLED": agent_cfg.tool_result_clearing_enabled,
        "TOOL_RESULT_CLEARING_KEEP_TOOL_USES": agent_cfg.tool_result_clearing_keep_tool_uses,
        "TOOL_RESULT_CLEARING_CLEAR_AT_LEAST_CHARS": (agent_cfg.tool_result_clearing_clear_at_least_chars),
        "TOOL_RESULT_CLEARING_CLEAR_ABOVE_INPUT_TOKENS": (agent_cfg.tool_result_clearing_clear_above_input_tokens),
        # Thinking-model settings (``spec.runtime.reasoning``) and the raw
        # request-body escape hatch (``spec.runtime.model_settings``).
        # Turned into ``ModelSettings`` by ``core.model_settings_for``.
        "REASONING_THINKING": agent_cfg.reasoning_thinking,
        "REASONING_BUDGET_TOKENS": agent_cfg.reasoning_budget_tokens,
        "REASONING_DISPLAY": agent_cfg.reasoning_display,
        "REASONING_EFFORT": agent_cfg.reasoning_effort,
        "REASONING_EMIT": agent_cfg.reasoning_emit,
        "LLM_EXTRA_BODY": agent_cfg.llm_extra_body,
        # Optional generic lifecycle hooks (dotted import paths).  ``None``
        # unless the instance authored ``spec.runtime.finalize`` / ``reset``.
        # Consumed by the ``@invoke`` / ``@stream`` path in
        # ``payloads.agent.runtime.agent`` to reset per-request state and
        # merge structured output into ``custom_outputs``.
        "FINALIZE_CALLABLE": agent_cfg.finalize_callable,
        "RESET_CALLABLE": agent_cfg.reset_callable,
        # Optional typed A2A config.  ``None`` when the instance has not
        # authored ``spec.integrations.a2a.card`` in payload.yml; the App
        # host falls back to its built-in default card in that case.
        # Stored as the live ``A2AConfig`` model (not a dict) so the
        # consumer can use the typed accessor in ``payloads.agent.config``.
        "A2A": agent_cfg.a2a,
    }

    if skills_result is not None:
        config["SYSTEM_PROMPT"] = skills_result["system_prompt"]
        config["SKILL_TOOLS"] = skills_result["tools"]
        config["SKILL_NAMES"] = skills_result["skill_names"]
        # Lazy-skill-loading passthrough (issue #98).  PR 1/2 only
        # surfaces these to the runtime; the actual ``_load_skill``
        # tool that consumes ``SKILL_INDEX`` lands in PR 2/2.
        # ``SKILL_LOADING`` is always set ("eager" when no
        # ``skill_index.json`` is on disk) so downstream code can
        # branch without optional-key guards.
        config["SKILL_LOADING"] = skills_result.get("skill_loading", "eager")
        config["SKILL_INDEX"] = skills_result.get("skill_index")
    else:
        config["SKILL_LOADING"] = agent_cfg.skill_loading
        config["SKILL_INDEX"] = None

    return config


def _assemble_skills_if_present(
    instance_dir: Path,
    variables: dict[str, str] | None = None,  # noqa: ARG001 (runtime signature; variables land in _assembled/ at deploy time)
) -> dict[str, Any] | None:
    """Read the pre-assembled skills output for an instance.

    Issue #83 (68-D): runtime code MUST NOT import the deploy-side
    skills assembler.  Instead, the skills provision step writes
    ``<instance>/skills/_assembled/system_prompt.md`` and
    ``<instance>/skills/_assembled/skill_tools.json`` as part of the
    deploy step (``payloads.agent.deploy.skills.step.SkillsStep``).  This
    function reads those files; if they're missing, skills haven't
    been deployed yet and we return None.

    Issue #98 (PR 1/2): when the brick ran the assembler in
    ``skill_loading: lazy`` mode, an additional
    ``_assembled/skill_index.json`` file is present.  We read it here
    purely as passthrough -- the runtime tool that turns
    ``SKILL_INDEX`` into a ``_load_skill`` callable lands in PR 2/2.

    The signature accepts *variables* for backwards compatibility with
    callers but does not use it -- all substitution happens at deploy
    time and is baked into the assembled files.
    """
    assembled = resolve_payload_subdir(instance_dir, "skills") / "_assembled"
    prompt_path = assembled / "system_prompt.md"
    tools_path = assembled / "skill_tools.json"

    if not prompt_path.exists() or not tools_path.exists():
        return None

    import json

    tools_data = json.loads(tools_path.read_text(encoding="utf-8"))

    skill_loading = "eager"
    skill_index: dict[str, str] | None = None
    index_path = assembled / "skill_index.json"
    if index_path.exists():
        index_data = json.loads(index_path.read_text(encoding="utf-8"))
        skill_loading = index_data.get("skill_loading", "lazy")
        skill_index = index_data.get("index")

    return {
        "system_prompt": prompt_path.read_text(encoding="utf-8"),
        "tools": tools_data.get("tools", []),
        "skill_names": tools_data.get("skill_names", []),
        "domain_names": tools_data.get("domain_names", []),
        "skill_loading": skill_loading,
        "skill_index": skill_index,
    }
