"""The deployed agent App, located through the published ``app_url`` and the instance's payload.yml."""

from __future__ import annotations

from typing import Any

import pytest

from agent_foundry.testing.live import LiveTarget
from agent_foundry.testing.readiness import app_origin, authenticated_post_json
from payloads.agent.config import AgentConfig, agent_config_kwargs_from_spec, load_agent_spec
from payloads.agent.contracts import AgentPayloadSpec

_PROMPT = "Reply with the single word hello."
# Asks the App to return the MLflow trace id in ``metadata.trace_id``.
_TRACE_ID_HEADER = "x-mlflow-return-trace-id"


@pytest.fixture(scope="session")
def agent_spec(live_target: LiveTarget) -> AgentPayloadSpec:
    return load_agent_spec(live_target.instance_dir)


@pytest.fixture(scope="session")
def llm_endpoint_name(agent_spec: AgentPayloadSpec) -> str:
    return AgentConfig(**agent_config_kwargs_from_spec(agent_spec)).llm_endpoint


@pytest.fixture(scope="session")
def app_url(live_variables: dict[str, str]) -> str:
    url = live_variables.get("app_url")
    assert url, "app brick published no app_url"
    return app_origin(url)


@pytest.fixture(scope="session")
def invocation(live_ws: Any, app_url: str) -> dict[str, Any]:
    """One traced ``/invocations`` reply, shared by the response and tracing checks."""
    return authenticated_post_json(
        live_ws,
        f"{app_url}/invocations",
        {"input": [{"role": "user", "content": _PROMPT}], "stream": False},
        headers={_TRACE_ID_HEADER: "true"},
    )
