"""``_request_skills``: every skill source behind one ``_load_skill``.

:mod:`payloads.agent.runtime.agent` calls ``init_agent_module()`` at import,
so it is patched to a no-op first (see ``test_finalize_hook``).
"""

from __future__ import annotations

import json
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mlflow.types.responses import ResponsesAgentRequest

import payloads.agent.runtime.runtime_config as _runtime_config
from payloads.agent.contracts import UserSkills
from payloads.agent.runtime.gateway_selection import Selections
from payloads.agent.runtime.gateway_skills import NO_GATEWAY_SKILLS, GatewaySkills
from payloads.agent.runtime.lazy_skills import LOAD_SKILL_TOOL_NAME, load_skill_function_tool
from payloads.agent.runtime.user_skills import SAVE_SKILL_TOOL_NAME

with patch.object(_runtime_config, "init_agent_module", lambda: None):
    from payloads.agent.runtime import agent

LAKEBASE = {"USER_SKILLS": UserSkills(store="lakebase")}


def _tool(name: str, body: str = "") -> Any:
    tool = load_skill_function_tool(AsyncMock(return_value=body))
    tool.name = name
    return tool


def _request() -> ResponsesAgentRequest:
    picked = {"fqn": "lakebase:mine", "display_name": "mine", "description": "Use it.", "body": "My steps."}
    return ResponsesAgentRequest(input=[{"role": "user", "content": "hi"}], custom_inputs={"user_skills": [picked]})


async def _request_skills(config: dict[str, Any], tools: list[Any], gateway: GatewaySkills) -> tuple[list[Any], Any]:
    with (
        patch.object(agent, "prepare_gateway_skills", AsyncMock(return_value=gateway)),
        patch.object(agent, "get_user_workspace_client", return_value=None),
    ):
        return await agent._request_skills(config, MagicMock(), _request(), tools, Selections())


async def _load(skills: GatewaySkills, skill_id: str) -> str:
    (load_tool,) = [tool for tool in skills.tools if tool.name == LOAD_SKILL_TOOL_NAME]
    return cast("str", await load_tool.on_invoke_tool(cast(Any, None), json.dumps({"skill_id": skill_id})))


@pytest.mark.asyncio
class TestRequestSkills:
    async def test_without_skill_stores_tools_are_unchanged(self) -> None:
        tools = [_tool("other"), _tool(LOAD_SKILL_TOOL_NAME, "repo body")]

        agent_tools, skills = await _request_skills({}, tools, NO_GATEWAY_SKILLS)

        assert agent_tools == tools
        assert skills is NO_GATEWAY_SKILLS

    async def test_lakebase_next_to_repo_lazy_skills_shares_one_load_skill(self) -> None:
        other = _tool("other")

        agent_tools, skills = await _request_skills(
            LAKEBASE, [other, _tool(LOAD_SKILL_TOOL_NAME, "repo body")], NO_GATEWAY_SKILLS
        )

        assert agent_tools == [other]
        assert [tool.name for tool in skills.tools] == [LOAD_SKILL_TOOL_NAME, SAVE_SKILL_TOOL_NAME]
        assert await _load(skills, "sales/triage") == "repo body"
        assert "My steps." in await _load(skills, "lakebase:mine")

    async def test_lakebase_next_to_gateway_skills_keeps_their_block_and_loader(self) -> None:
        gateway = GatewaySkills(
            block="## Skills",
            rows=(("t.s.shared", "shared", "Use it."),),
            tools=(_tool(LOAD_SKILL_TOOL_NAME, "gateway body"),),
        )

        _, skills = await _request_skills(LAKEBASE, [], gateway)

        assert skills.block == "## Skills"
        assert "`t.s.shared`" in skills.index
        assert "`lakebase:mine`" in skills.index
        assert await _load(skills, "t.s.shared") == "gateway body"

    async def test_gateway_picks_replace_the_repo_load_skill(self) -> None:
        other = _tool("other")
        gateway = GatewaySkills(rows=(("t.s.pick", "pick", "Use it."),), tools=(_tool(LOAD_SKILL_TOOL_NAME),))

        agent_tools, skills = await _request_skills({}, [other, _tool(LOAD_SKILL_TOOL_NAME)], gateway)

        assert agent_tools == [other]
        assert skills is gateway
