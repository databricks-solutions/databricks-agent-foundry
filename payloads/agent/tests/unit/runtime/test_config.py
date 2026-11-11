"""Tests for payloads/agent/runtime/config.py -- load_agent_config.

Per-instance runtime config is sourced from the agent payload's ``spec``
in ``payloads/agent/payload.yml`` (the single config contract).  Skills
are read from the deploy-time assembled artefacts under
``payloads/agent/skills/_assembled/``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from payloads.agent.config import AgentConfig
from payloads.agent.runtime.config import load_agent_config

# Reference defaults from the Pydantic model
_DEFAULTS = AgentConfig()
LLM_ENDPOINT_NAME = _DEFAULTS.llm_endpoint
SYSTEM_PROMPT = _DEFAULTS.system_prompt
MAX_TOOL_CALLS_PER_TURN = _DEFAULTS.max_tool_calls_per_turn
MAX_ITERATIONS = _DEFAULTS.max_iterations


def _write_spec(
    instance_dir: Path,
    runtime: dict[str, Any] | None = None,
    *,
    spec: dict[str, Any] | None = None,
) -> None:
    """Write ``payloads/agent/payload.yml`` with the given spec body."""
    payload_dir = instance_dir / "payloads" / "agent"
    payload_dir.mkdir(parents=True, exist_ok=True)
    body = spec if spec is not None else {"runtime": runtime or {}}
    (payload_dir / "payload.yml").write_text(yaml.dump({"payload": {"adapter": "agent", "spec": body}}))


def test_defaults_when_no_instance_dir() -> None:
    config = load_agent_config(None)
    assert config["LLM_ENDPOINT_NAME"] == LLM_ENDPOINT_NAME
    assert config["SYSTEM_PROMPT"] == SYSTEM_PROMPT
    assert config["MAX_TOOL_CALLS_PER_TURN"] == MAX_TOOL_CALLS_PER_TURN
    assert config["MAX_ITERATIONS"] == MAX_ITERATIONS


def test_tool_sources_default_to_local_trees() -> None:
    config = load_agent_config(None)
    assert config["LLM_KIND"] == "serving_endpoint"
    assert config["REPOSITORY_SKILLS"] is True
    assert config["GATEWAY_MCP"] is None
    assert config["GATEWAY_SKILLS"] is None


def test_gateway_spec_reaches_runtime_config(tmp_path: Path) -> None:
    _write_spec(
        tmp_path,
        {
            "model": {"kind": "model_service", "name": "system.ai.claude-sonnet-4-5"},
            "tools": {
                "mcp": {"gateway": {"pinned": [{"fqn": "a.b.c", "identity": "obo"}]}},
                "skills": {"source": "unity_gateway"},
            },
        },
    )
    config = load_agent_config(tmp_path)
    assert config["LLM_ENDPOINT_NAME"] == "system.ai.claude-sonnet-4-5"
    assert config["LLM_KIND"] == "model_service"
    assert config["GATEWAY_MCP"].pinned[0].identity == "obo"
    assert config["REPOSITORY_SKILLS"] is False
    assert config["GATEWAY_SKILLS"] is not None


def test_gateway_skill_loading_comes_from_the_spec(tmp_path: Path) -> None:
    _write_spec(tmp_path, {"tools": {"skills": {"source": "unity_gateway", "loading": "lazy"}}})
    assert load_agent_config(tmp_path)["SKILL_LOADING"] == "lazy"


def test_defaults_when_no_payload_yml(tmp_path: Path) -> None:
    config = load_agent_config(tmp_path)
    assert config["LLM_ENDPOINT_NAME"] == LLM_ENDPOINT_NAME


def test_overrides_from_spec(tmp_path: Path) -> None:
    _write_spec(
        tmp_path,
        {
            "model": {"kind": "serving_endpoint", "name": "custom-model"},
            "system_prompt": "You are a test agent.",
            "max_iterations": 3,
        },
    )

    config = load_agent_config(tmp_path)
    assert config["LLM_ENDPOINT_NAME"] == "custom-model"
    assert config["SYSTEM_PROMPT"] == "You are a test agent."
    assert config["MAX_ITERATIONS"] == 3
    assert config["MAX_TOOL_CALLS_PER_TURN"] == MAX_TOOL_CALLS_PER_TURN


def test_partial_overrides(tmp_path: Path) -> None:
    _write_spec(tmp_path, {"max_iterations": 1})

    config = load_agent_config(tmp_path)
    assert config["MAX_ITERATIONS"] == 1
    assert config["LLM_ENDPOINT_NAME"] == LLM_ENDPOINT_NAME
    assert config["SYSTEM_PROMPT"] == SYSTEM_PROMPT


def test_empty_payload_yml(tmp_path: Path) -> None:
    payload_dir = tmp_path / "payloads" / "agent"
    payload_dir.mkdir(parents=True)
    (payload_dir / "payload.yml").write_text("")

    config = load_agent_config(tmp_path)
    assert config["LLM_ENDPOINT_NAME"] == LLM_ENDPOINT_NAME


def test_skills_loaded_from_assembled_files(tmp_path: Path) -> None:
    """Runtime reads pre-assembled skills files (issue #83, 68-D).

    The runtime no longer invokes the deploy-side assembler; the
    ``skills`` provision step writes ``_assembled/system_prompt.md`` and
    ``_assembled/skill_tools.json`` at deploy time under the payload's
    ``skills/`` dir, and ``load_agent_config`` reads those files here.
    """
    _write_spec(tmp_path, {"system_prompt": "Base agent prompt (ignored; _assembled overrides)."})

    assembled = tmp_path / "payloads" / "agent" / "skills" / "_assembled"
    assembled.mkdir(parents=True)
    (assembled / "system_prompt.md").write_text(
        "Base agent prompt.\n\n## Skill: test_skill\n\nA test skill for validation.\n"
    )
    (assembled / "skill_tools.json").write_text(
        json.dumps(
            {
                "tools": [
                    {
                        "name": "test_tool",
                        "type": "uc_function",
                        "function_name": "cat.sch.test_tool",
                        "description": "test",
                    }
                ],
                "skill_names": ["test_skill"],
                "domain_names": [],
            }
        )
    )

    config = load_agent_config(tmp_path)
    assert "Skill: test_skill" in config["SYSTEM_PROMPT"]
    assert "A test skill for validation." in config["SYSTEM_PROMPT"]
    assert config["SKILL_NAMES"] == ["test_skill"]
    assert len(config["SKILL_TOOLS"]) == 1
    assert config["SKILL_TOOLS"][0]["name"] == "test_tool"


def test_skills_absent_when_assembled_files_missing(tmp_path: Path) -> None:
    """If deploy hasn't run yet (no _assembled/ dir), runtime falls
    back to the bare agent prompt and empty tool list."""
    _write_spec(tmp_path, {"system_prompt": "Base agent prompt."})

    config = load_agent_config(tmp_path)
    assert config["SYSTEM_PROMPT"] == "Base agent prompt."
    assert "SKILL_NAMES" not in config


def test_inject_time_user_default_true() -> None:
    config = load_agent_config(None)
    assert config["INJECT_TIME_USER"] is True


def test_inject_time_user_opt_out(tmp_path: Path) -> None:
    _write_spec(tmp_path, {"inject_time_user": False})
    config = load_agent_config(tmp_path)
    assert config["INJECT_TIME_USER"] is False


def test_mcp_only_default_true() -> None:
    """The framework default is MCP-only; direct factories opt-in via spec."""
    config = load_agent_config(None)
    assert config["MCP_ONLY"] is True


def test_mcp_only_opt_out(tmp_path: Path) -> None:
    """Instances can opt out by setting ``tools.mcp_only: false`` to keep
    direct-mode Genie / Vector Search / UC Function tools."""
    _write_spec(tmp_path, {"tools": {"mcp_only": False}})
    config = load_agent_config(tmp_path)
    assert config["MCP_ONLY"] is False


def test_lifecycle_hooks_default_to_none() -> None:
    config = load_agent_config(None)
    assert config["FINALIZE_CALLABLE"] is None
    assert config["RESET_CALLABLE"] is None


def test_lifecycle_hooks_emitted_from_spec(tmp_path: Path) -> None:
    _write_spec(tmp_path, {"finalize": "pkg.hooks.finalize", "reset": "pkg.hooks.reset"})
    config = load_agent_config(tmp_path)
    assert config["FINALIZE_CALLABLE"] == "pkg.hooks.finalize"
    assert config["RESET_CALLABLE"] == "pkg.hooks.reset"


def test_prompt_cache_defaults_are_emitted() -> None:
    config = load_agent_config(None)
    assert config["PROMPT_CACHE_ENABLED"] is True
    assert config["PROMPT_CACHE_STATIC_TTL"] == "1h"
    assert config["PROMPT_CACHE_ROLLING_TTL"] is None


def test_prompt_cache_authored_values_are_emitted(tmp_path: Path) -> None:
    _write_spec(tmp_path, {"context": {"prompt_cache": {"enabled": False, "static_ttl": "5m"}}})
    config = load_agent_config(tmp_path)
    assert config["PROMPT_CACHE_ENABLED"] is False
    assert config["PROMPT_CACHE_STATIC_TTL"] == "5m"


def test_data_universe_defaults_are_emitted() -> None:
    config = load_agent_config(None)
    assert config["DATA_UNIVERSE_ENABLED"] is False
    assert config["DATA_UNIVERSE_FUNCTION"] == "get_data_landscape"
    assert config["DATA_UNIVERSE_HEADING"] == "## Data Universe"
    assert config["DATA_UNIVERSE_MAX_ROWS"] == 200
    assert config["DATA_UNIVERSE_TIMEOUT_SECONDS"] == 30
    assert config["DATA_UNIVERSE_REFRESH_MINUTES"] == 60


def test_tool_result_clearing_defaults_are_emitted() -> None:
    config = load_agent_config(None)
    assert config["TOOL_RESULT_CLEARING_ENABLED"] is True
    assert config["TOOL_RESULT_CLEARING_KEEP_TOOL_USES"] == 3
    assert config["TOOL_RESULT_CLEARING_CLEAR_AT_LEAST_CHARS"] == 300_000
    assert config["TOOL_RESULT_CLEARING_CLEAR_ABOVE_INPUT_TOKENS"] == 150_000


def test_tool_result_clearing_authored_values_are_emitted(tmp_path: Path) -> None:
    _write_spec(
        tmp_path,
        {
            "context": {
                "tool_result_clearing": {
                    "enabled": False,
                    "keep_tool_uses": 6,
                    "clear_at_least_chars": 10000,
                    "clear_above_input_tokens": 60000,
                }
            }
        },
    )
    config = load_agent_config(tmp_path)
    assert config["TOOL_RESULT_CLEARING_ENABLED"] is False
    assert config["TOOL_RESULT_CLEARING_KEEP_TOOL_USES"] == 6
    assert config["TOOL_RESULT_CLEARING_CLEAR_AT_LEAST_CHARS"] == 10000
    assert config["TOOL_RESULT_CLEARING_CLEAR_ABOVE_INPUT_TOKENS"] == 60000


def test_data_universe_authored_values_are_emitted(tmp_path: Path) -> None:
    _write_spec(
        tmp_path,
        {"context": {"data_universe": {"enabled": True, "function": "c.s.fn", "max_rows": 25}}},
    )
    config = load_agent_config(tmp_path)
    assert config["DATA_UNIVERSE_ENABLED"] is True
    assert config["DATA_UNIVERSE_FUNCTION"] == "c.s.fn"
    assert config["DATA_UNIVERSE_MAX_ROWS"] == 25


def test_no_skills_dir_unchanged(tmp_path: Path) -> None:
    _write_spec(tmp_path, {"system_prompt": "Base prompt only."})

    config = load_agent_config(tmp_path)
    assert config["SYSTEM_PROMPT"] == "Base prompt only."
    assert "SKILL_NAMES" not in config


# ---------------------------------------------------------------------------
# Lazy skill loading -- issue #98 passthrough
# ---------------------------------------------------------------------------


def _seed_assembled(tmp_path: Path, *, with_skill_index: bool = False, skill_loading: str = "lazy") -> None:
    """Materialise what the skills brick writes at deploy time."""
    _write_spec(tmp_path, {"system_prompt": "ignored at runtime; assembled prompt wins"})

    assembled = tmp_path / "payloads" / "agent" / "skills" / "_assembled"
    assembled.mkdir(parents=True)
    (assembled / "system_prompt.md").write_text("# assembled\n")
    (assembled / "skill_tools.json").write_text(
        json.dumps(
            {
                "tools": [],
                "skill_names": ["s1"],
                "domain_names": [],
            }
        )
    )

    if with_skill_index:
        (assembled / "skill_index.json").write_text(
            json.dumps(
                {
                    "skill_loading": skill_loading,
                    "index": {"s1": "s1/SKILL.md"},
                }
            )
        )


def test_skill_loading_defaults_to_eager_when_no_instance_dir() -> None:
    """``SKILL_LOADING`` is always set so consumers can branch without
    optional-key guards."""
    config = load_agent_config(None)
    assert config["SKILL_LOADING"] == "eager"
    assert config["SKILL_INDEX"] is None


def test_skill_loading_eager_when_assembled_present_but_no_index(tmp_path: Path) -> None:
    """Eager-only deploy: brick wrote ``system_prompt.md`` +
    ``skill_tools.json`` but no ``skill_index.json``.  Bridge defaults
    ``SKILL_LOADING`` to ``"eager"`` and ``SKILL_INDEX`` to ``None``."""
    _seed_assembled(tmp_path, with_skill_index=False)
    config = load_agent_config(tmp_path)
    assert config["SKILL_LOADING"] == "eager"
    assert config["SKILL_INDEX"] is None


def test_skill_index_passthrough_when_present(tmp_path: Path) -> None:
    """Lazy-mode deploy: bridge surfaces both keys verbatim from the
    assembled artefact."""
    _seed_assembled(tmp_path, with_skill_index=True, skill_loading="lazy")
    config = load_agent_config(tmp_path)
    assert config["SKILL_LOADING"] == "lazy"
    assert config["SKILL_INDEX"] == {"s1": "s1/SKILL.md"}


def test_skill_index_none_when_absent(tmp_path: Path) -> None:
    """No instance config + no assembled dir -> still safe defaults."""
    payload_dir = tmp_path / "payloads" / "agent"
    payload_dir.mkdir(parents=True)
    (payload_dir / "payload.yml").write_text("")
    config = load_agent_config(tmp_path)
    assert config["SKILL_LOADING"] == "eager"
    assert config["SKILL_INDEX"] is None


def test_reasoning_defaults_are_emitted() -> None:
    config = load_agent_config(None)
    assert config["REASONING_THINKING"] is None
    assert config["REASONING_BUDGET_TOKENS"] is None
    assert config["REASONING_DISPLAY"] is None
    assert config["REASONING_EFFORT"] is None
    assert config["REASONING_EMIT"] is True
    assert config["LLM_EXTRA_BODY"] is None


def test_reasoning_authored_values_are_emitted(tmp_path: Path) -> None:
    _write_spec(
        tmp_path,
        {
            "reasoning": {"thinking": "adaptive", "display": "omitted", "effort": "high", "emit": False},
            "model_settings": {"extra_body": {"max_tokens": 16000}},
        },
    )
    config = load_agent_config(tmp_path)
    assert config["REASONING_THINKING"] == "adaptive"
    assert config["REASONING_DISPLAY"] == "omitted"
    assert config["REASONING_EFFORT"] == "high"
    assert config["REASONING_EMIT"] is False
    assert config["LLM_EXTRA_BODY"] == {"max_tokens": 16000}
