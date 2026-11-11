"""Lakebase user skills: input checks, the merged ``_load_skill``, and the cached prefix."""

from __future__ import annotations

import json
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest

from payloads.agent.contracts import UserSkills
from payloads.agent.runtime.core import create_agent
from payloads.agent.runtime.gateway_skills import NO_GATEWAY_SKILLS, GatewaySkills
from payloads.agent.runtime.lazy_skills import LOAD_SKILL_TOOL_NAME
from payloads.agent.runtime.prompt_cache import CONTEXT_SENTINEL, USER_TAIL_SENTINEL
from payloads.agent.runtime.user_skills import (
    MAX_USER_SKILL_INPUTS,
    SAVE_SKILL_TOOL_NAME,
    SkillSaver,
    UserSkillLoader,
    parse_user_skills,
    prepare_user_skills,
)

LIMITS = UserSkills(store="lakebase", max_skills=2, max_skill_md_chars=50)


def _skill(slug: str, body: str = "Steps.", description: str = "Use it.") -> dict[str, str]:
    return {"fqn": f"lakebase:{slug}", "display_name": slug, "description": description, "body": body}


def _inputs(*skills: dict[str, str]) -> dict[str, Any]:
    return {"user_skills": list(skills)}


def _config(**extra: Any) -> dict[str, Any]:
    return {
        "SYSTEM_PROMPT": "Base.",
        "LLM_ENDPOINT_NAME": "test-model",
        "INJECT_TIME_USER": True,
        "USER_SKILLS": LIMITS,
        **extra,
    }


class TestParseUserSkills:
    def test_missing_input_means_none_picked(self) -> None:
        parsed = parse_user_skills(None, LIMITS)
        assert (parsed.skills, parsed.dropped, parsed.rejected) == ((), (), None)

    def test_keeps_skills_within_limits_in_order(self) -> None:
        parsed = parse_user_skills(_inputs(_skill("a"), _skill("b")), LIMITS)
        assert [skill.fqn for skill in parsed.skills] == ["lakebase:a", "lakebase:b"]

    def test_drops_duplicates_oversized_bodies_and_skills_over_the_limit(self) -> None:
        parsed = parse_user_skills(
            _inputs(_skill("a"), _skill("a"), _skill("big", body="x" * 51), _skill("b"), _skill("c")),
            LIMITS,
        )
        assert [skill.fqn for skill in parsed.skills] == ["lakebase:a", "lakebase:b"]
        assert parsed.dropped == (
            "lakebase:a|duplicate",
            "lakebase:big|over_char_limit",
            "lakebase:c|over_skill_limit",
        )

    @pytest.mark.parametrize(
        "raw",
        [
            [{**_skill("a"), "fqn": "main.skills.a"}],
            [{**_skill("a"), "fqn": "lakebase:Bad"}],
            [{**_skill("a"), "owner": "someone"}],
            [{**_skill("a"), "body": ""}],
            [_skill(f"s{i}") for i in range(MAX_USER_SKILL_INPUTS + 1)],
            {"fqn": "lakebase:a"},
        ],
    )
    def test_rejects_malformed_input_whole(self, raw: Any) -> None:
        parsed = parse_user_skills({"user_skills": raw}, LIMITS)
        assert parsed.skills == ()
        assert parsed.rejected is not None


class TestUserSkillLoader:
    @pytest.mark.asyncio
    async def test_loads_a_user_skill_in_the_runbook_envelope(self) -> None:
        loader = UserSkillLoader(parse_user_skills(_inputs(_skill("a", body="Do it.")), LIMITS).skills, None)

        response = await loader(None, json.dumps({"skill_id": "lakebase:a"}))

        assert response.startswith('<skill_runbook skill_id="lakebase:a"')
        assert "Do it." in response

    @pytest.mark.asyncio
    async def test_refuses_skills_not_picked_this_request(self) -> None:
        loader = UserSkillLoader(parse_user_skills(_inputs(_skill("a")), LIMITS).skills, None)

        response = await loader(None, json.dumps({"skill_id": "lakebase:other"}))

        assert response.startswith("Error:")

    @pytest.mark.asyncio
    async def test_hands_repo_skill_ids_to_the_repo_loader(self) -> None:
        repo_loader = AsyncMock(return_value="repo body")
        loader = UserSkillLoader((), repo_loader)
        args = json.dumps({"skill_id": "sql/review"})

        assert await loader("ctx", args) == "repo body"
        repo_loader.assert_awaited_once_with("ctx", args)

    @pytest.mark.asyncio
    async def test_keeps_lakebase_ids_away_from_the_repo_loader(self) -> None:
        repo_loader = AsyncMock(return_value="repo body")
        loader = UserSkillLoader((), repo_loader)

        response = await loader(None, json.dumps({"skill_id": "lakebase:a"}))

        assert response.startswith("Error:")
        repo_loader.assert_not_awaited()


class TestSkillSaver:
    @pytest.mark.asyncio
    async def test_accepts_a_valid_draft_without_writing(self) -> None:
        draft = {"slug": "weekly-report", "description": "Use it.", "body": "Steps."}

        response = await SkillSaver(LIMITS)(None, json.dumps(draft))

        assert response.startswith("Saving skill `lakebase:weekly-report`")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("draft", "problem"),
        [
            ({"slug": "Weekly", "description": "d", "body": "b"}, "slug"),
            ({"slug": "a", "description": "d", "body": "b", "owner": "x"}, "owner"),
            ({"slug": "a", "description": "  ", "body": "b"}, "description"),
            ({"slug": "a", "description": "d", "body": "x" * 51}, "limit is 50"),
            ({"slug": "a", "description": "d"}, "body"),
        ],
    )
    async def test_tells_the_model_what_to_fix(self, draft: dict[str, str], problem: str) -> None:
        response = await SkillSaver(LIMITS)(None, json.dumps(draft))

        assert response.startswith("Error: the skill was not saved")
        assert problem in response

    @pytest.mark.asyncio
    async def test_refuses_non_json_arguments(self) -> None:
        assert (await SkillSaver(LIMITS)(None, "{oops")).startswith("Error:")


class TestPrepareUserSkills:
    def test_off_without_user_skills(self) -> None:
        assert prepare_user_skills({}, _inputs(_skill("a")), NO_GATEWAY_SKILLS, None) is None

    def test_registers_load_and_save_skill_even_with_nothing_picked(self) -> None:
        skills = prepare_user_skills(_config(), None, NO_GATEWAY_SKILLS, None)

        assert skills is not None
        assert skills.index == ""
        assert [tool.name for tool in skills.tools] == [LOAD_SKILL_TOOL_NAME, SAVE_SKILL_TOOL_NAME]

    def test_save_skill_tells_the_model_indexed_user_skills_are_editable(self) -> None:
        skills = prepare_user_skills(_config(), None, NO_GATEWAY_SKILLS, None)

        assert skills is not None
        assert "`lakebase:<slug>`; pass that slug to update one" in skills.tools[1].description

    def test_keeps_the_agent_skills_ahead_of_the_users(self) -> None:
        agent_skills = GatewaySkills(block="## Skills", rows=(("t.s.shared", "shared", "Use it."),))

        skills = prepare_user_skills(_config(), _inputs(_skill("a")), agent_skills, None)

        assert skills is not None
        assert skills.block == "## Skills"
        assert skills.index.index("`t.s.shared`") < skills.index.index("`lakebase:a`")

    @pytest.mark.asyncio
    async def test_load_skill_hands_other_ids_to_the_fallback(self) -> None:
        fallback = AsyncMock(return_value="gateway body")
        skills = prepare_user_skills(_config(), _inputs(_skill("a")), NO_GATEWAY_SKILLS, fallback)
        assert skills is not None

        result = await skills.tools[0].on_invoke_tool(cast(Any, None), json.dumps({"skill_id": "t.s.shared"}))

        assert result == "gateway body"

    def test_traces_names_and_hashes_never_skill_text(self) -> None:
        with patch("mlflow.start_span") as start_span:
            prepare_user_skills(_config(), _inputs(_skill("a", body="Secret steps.")), NO_GATEWAY_SKILLS, None)

        attributes = start_span.return_value.__enter__.return_value.set_attributes.call_args.args[0]
        assert [label.split("|")[0] for label in attributes["user_skills.indexed"]] == ["lakebase:a"]
        assert "Secret steps." not in json.dumps(attributes)


def _tool_signature(tool: Any) -> tuple[str, str, str]:
    return tool.name, tool.description, json.dumps(tool.params_json_schema, sort_keys=True)


class TestPromptCachePrefix:
    """User skills live outside the cached prefix, so the 1h cache is shared across users."""

    def _agent_for(self, user: str, *skills: dict[str, str]) -> Any:
        config = _config(_resolved_username=user)
        user_skills = prepare_user_skills(config, _inputs(*skills), NO_GATEWAY_SKILLS, None)
        assert user_skills is not None
        return create_agent(config, tools=[], data_universe="## DU", gateway_skills=user_skills)

    def test_two_users_share_the_cached_prefix_and_tools(self) -> None:
        alice = self._agent_for("alice@example.com", _skill("weekly-report", body="Alice's steps."))
        bob = self._agent_for("bob@example.com")

        alice_body, _, alice_tail = cast("str", alice.instructions).partition(USER_TAIL_SENTINEL)
        bob_body = cast("str", bob.instructions).partition(CONTEXT_SENTINEL)[0]
        assert alice_body == bob_body == "Base.\n\n## DU\n\n"
        assert "`lakebase:weekly-report`" in alice_tail
        assert [_tool_signature(t) for t in alice.tools] == [_tool_signature(t) for t in bob.tools]
