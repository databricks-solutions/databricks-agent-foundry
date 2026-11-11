"""Tests for the ``spec`` -> App env projection.

``env_overrides_from_spec`` is the single hop between the payload's typed
``spec`` and the environment the App container boots with, so a toggle
that is authored but never projected fails silently -- the UI simply
never sees it.  These tests pin the direction of each mapping.

The complementary drift guards live in
``bricks/app/tests/unit/test_app_yaml_env_sync.py``, which asserts these keys
are registered in ``agent_foundry.env`` and are *not* double-authored in
the infra-only ``bricks/app/app.yaml``.
"""

from __future__ import annotations

import pytest

from payloads.agent.contracts import AgentPayloadSpec
from payloads.agent.deploy.config import env_overrides_from_spec


class TestEnvOverridesFromSpec:
    def test_defaults_render_as_boolean_strings(self) -> None:
        env = env_overrides_from_spec(AgentPayloadSpec())
        assert env["ENABLE_CHARTS"] == "true"
        assert env["ENABLE_FEEDBACK"] == "true"
        assert env["ENABLE_FILE_UPLOAD"] == "false"
        assert env["ENABLE_IMAGE_INPUT"] == "false"
        assert env["ENABLE_A2A"] == "false"
        assert env["ENABLE_MCP_SERVER"] == "false"
        assert env["ENABLE_RESOURCES_SIDEBAR"] == "false"
        assert env["FILE_UPLOAD_MAX_MB"] == "10"
        assert env["CHAT_HISTORY_SIDEBAR_DAYS"] == "30"
        assert env["CHAT_HISTORY_RETENTION_DAYS"] == "365"

    def test_user_skills_project_store_and_limits(self) -> None:
        assert env_overrides_from_spec(AgentPayloadSpec())["ENABLE_USER_SKILLS"] == "false"
        spec = AgentPayloadSpec.model_validate(
            {
                "runtime": {"tools": {"skills": {"user_skills": {"store": "lakebase", "max_skills": 5}}}},
                "ui": {"resources_sidebar": {"enabled": True}},
            }
        )
        env = env_overrides_from_spec(spec)
        assert (env["ENABLE_USER_SKILLS"], env["USER_SKILLS_MAX"], env["USER_SKILLS_MAX_MD_CHARS"]) == (
            "true",
            "5",
            "20000",
        )

    def test_unset_chat_history_days_project_as_off(self) -> None:
        spec = AgentPayloadSpec.model_validate({"ui": {"chat_history": {"sidebar_days": None, "retention_days": None}}})
        env = env_overrides_from_spec(spec)
        assert env["CHAT_HISTORY_SIDEBAR_DAYS"] == "0"
        assert env["CHAT_HISTORY_RETENTION_DAYS"] == "0"

    def test_resources_sidebar_projects_when_enabled(self) -> None:
        spec = AgentPayloadSpec.model_validate({"ui": {"resources_sidebar": {"enabled": True}}})
        assert env_overrides_from_spec(spec)["ENABLE_RESOURCES_SIDEBAR"] == "true"

    @pytest.mark.parametrize(
        ("enabled", "images", "expected"),
        [(True, True, "true"), (True, False, "false"), (False, True, "false")],
    )
    def test_image_input_requires_file_upload(self, enabled: bool, images: bool, expected: str) -> None:
        spec = AgentPayloadSpec.model_validate({"ui": {"file_upload": {"enabled": enabled, "images": images}}})
        assert env_overrides_from_spec(spec)["ENABLE_IMAGE_INPUT"] == expected

    @pytest.mark.parametrize(("expanded", "expected"), [(False, "false"), (True, "true")])
    def test_tool_output_expanded_projects_both_ways(self, expanded: bool, expected: str) -> None:
        spec = AgentPayloadSpec.model_validate({"ui": {"tool_output": {"expanded": expanded}}})
        assert env_overrides_from_spec(spec)["TOOL_OUTPUT_EXPANDED"] == expected

    @pytest.mark.parametrize("width", ["4xl", "5xl", "6xl", "7xl"])
    def test_chat_layout_width_projects_each_step(self, width: str) -> None:
        spec = AgentPayloadSpec.model_validate({"ui": {"chat_layout": {"width": width}}})
        assert env_overrides_from_spec(spec)["CHAT_MAX_WIDTH"] == width

    def test_chat_layout_defaults_to_4xl(self) -> None:
        # Existing instances keep today's column width until they opt in.
        assert env_overrides_from_spec(AgentPayloadSpec())["CHAT_MAX_WIDTH"] == "4xl"

    def test_tool_output_defaults_to_collapsed(self) -> None:
        # The chat UI treats an absent/false flag as collapsed, so the
        # default must not be truthy or every panel opens.
        assert env_overrides_from_spec(AgentPayloadSpec())["TOOL_OUTPUT_EXPANDED"] == "false"


class TestCompactionEnv:
    def test_defaults_follow_chat_history_with_derived_trigger(self) -> None:
        env = env_overrides_from_spec(AgentPayloadSpec())
        assert env["ENABLE_COMPACTION"] == "true"
        assert env["COMPACTION_TRIGGER_TOKENS"] == "120000"
        assert env["COMPACTION_KEEP_TURNS"] == "3"
        assert env["COMPACTION_INSTRUCTIONS"] == ""
        assert env["COMPACTION_MANUAL_ACTION"] == "false"

    def test_manual_action_is_opt_in_and_needs_compaction(self) -> None:
        manual = {"runtime": {"context": {"compaction": {"manual_action": True}}}}
        assert env_overrides_from_spec(AgentPayloadSpec.model_validate(manual))["COMPACTION_MANUAL_ACTION"] == "true"
        without_history = {**manual, "ui": {"chat_history": {"enabled": False}}}
        spec = AgentPayloadSpec.model_validate(without_history)
        assert env_overrides_from_spec(spec)["COMPACTION_MANUAL_ACTION"] == "false"

    def test_off_without_chat_history(self) -> None:
        spec = AgentPayloadSpec.model_validate({"ui": {"chat_history": {"enabled": False}}})
        assert env_overrides_from_spec(spec)["ENABLE_COMPACTION"] == "false"

    def test_explicit_off(self) -> None:
        spec = AgentPayloadSpec.model_validate({"runtime": {"context": {"compaction": {"enabled": False}}}})
        assert env_overrides_from_spec(spec)["ENABLE_COMPACTION"] == "false"

    def test_trigger_tracks_a_lowered_clearing_threshold(self) -> None:
        spec = AgentPayloadSpec.model_validate(
            {"runtime": {"context": {"tool_result_clearing": {"clear_above_input_tokens": 60_000}}}}
        )
        assert env_overrides_from_spec(spec)["COMPACTION_TRIGGER_TOKENS"] == "40000"

    def test_authored_values_project(self) -> None:
        compaction = {"trigger_input_tokens": 140_000, "keep_recent_turns": 5, "instructions": "Keep ticket IDs."}
        spec = AgentPayloadSpec.model_validate({"runtime": {"context": {"compaction": compaction}}})
        env = env_overrides_from_spec(spec)
        assert (env["COMPACTION_TRIGGER_TOKENS"], env["COMPACTION_KEEP_TURNS"], env["COMPACTION_INSTRUCTIONS"]) == (
            "140000",
            "5",
            "Keep ticket IDs.",
        )


class TestMemoryEnv:
    def test_defaults_are_off(self) -> None:
        env = env_overrides_from_spec(AgentPayloadSpec())
        assert (env["ENABLE_CHAT_PLAN"], env["PLAN_MAX_CHARS"]) == ("false", "8000")
        assert (env["ENABLE_AGENT_MEMORY"], env["AGENT_MEMORY_MAX_TOPIC_CHARS"]) == ("false", "8000")

    @staticmethod
    def _env(context: dict[str, object]) -> dict[str, str]:
        return env_overrides_from_spec(AgentPayloadSpec.model_validate({"runtime": {"context": context}}))

    def test_chat_entry_cleanup_follows_the_plan(self) -> None:
        assert self._env({"plan": {"enabled": True}})["ENABLE_CHAT_PLAN"] == "true"
        assert self._env({"memory": {"enabled": True}})["ENABLE_CHAT_PLAN"] == "false"

    def test_plan_size_limit_projects(self) -> None:
        assert self._env({"plan": {"enabled": True, "max_chars": 4000}})["PLAN_MAX_CHARS"] == "4000"

    def test_memory_projects_with_its_topic_limit(self) -> None:
        env = self._env({"memory": {"enabled": True, "max_entry_chars": 4000}})
        assert (env["ENABLE_AGENT_MEMORY"], env["AGENT_MEMORY_MAX_TOPIC_CHARS"]) == ("true", "4000")
