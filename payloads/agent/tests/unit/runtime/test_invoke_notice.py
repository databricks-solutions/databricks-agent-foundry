"""The ``@invoke`` response leads with the unavailable-tools notice.

:mod:`payloads.agent.runtime.agent` calls ``init_agent_module()`` at import,
so it is patched to a no-op first (see ``test_finalize_hook``).
"""

from __future__ import annotations

from unittest.mock import patch

from mlflow.types.responses import ResponsesAgentResponse

import payloads.agent.runtime.runtime_config as _runtime_config
from payloads.agent.runtime.stream_utils import build_message_item

with patch.object(_runtime_config, "init_agent_module", lambda: None):
    from payloads.agent.runtime import agent


def _response() -> ResponsesAgentResponse:
    return ResponsesAgentResponse(output=[build_message_item("answer")], custom_outputs={"k": 1})


def _texts(response: ResponsesAgentResponse) -> list[str]:
    return [item.model_dump()["content"][0]["text"] for item in response.output]


def test_empty_notice_leaves_response_unchanged() -> None:
    response = _response()

    assert agent._with_notice(response, "") is response


def test_notice_leads_and_custom_outputs_survive() -> None:
    response = agent._with_notice(_response(), "Some tools are unavailable")

    assert _texts(response) == ["Some tools are unavailable", "answer"]
    assert response.custom_outputs == {"k": 1}
