"""Tests for the outer-trace tags set on ``/invocations`` requests.

``payloads.agent.runtime.agent`` calls ``init_agent_module()`` at import
(Databricks auth), so it is patched to a no-op before the import.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import payloads.agent.runtime.runtime_config as _runtime_config

with patch.object(_runtime_config, "init_agent_module", lambda: None):
    from payloads.agent.runtime import agent


def _tags_for(config: dict[str, str]) -> dict[str, str]:
    update = MagicMock()
    with (
        patch.object(agent, "_trace_metadata", return_value={}),
        patch.object(agent, "_extract_user_question", return_value="q"),
        patch("mlflow.update_current_trace", update),
    ):
        agent._tag_outer_trace(SimpleNamespace(), config)  # type: ignore[arg-type]
    tags: dict[str, str] = update.call_args.kwargs["tags"]
    return tags


def test_invocations_trace_tagged_source_ui() -> None:
    assert _tags_for({}) == {"source": "ui"}


def test_prompt_tag_kept_alongside_source() -> None:
    tags = _tags_for({"PROMPT_NAME": "cat.sch.prompt", "PROMPT_VERSION": "3"})
    assert tags["source"] == "ui"
    assert json.loads(tags["mlflow.linkedPrompts"]) == [{"name": "cat.sch.prompt", "version": "3"}]
