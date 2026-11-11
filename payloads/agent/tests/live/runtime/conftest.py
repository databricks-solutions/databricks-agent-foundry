"""OpenAI clients on workspace serving endpoints and Unity Gateway.

These live tests call Model Serving from the CI/laptop process. They do
not use a deployed App or ``payload.yml``.  The Unity Gateway variants
run only when ``AGENT_FOUNDRY_TEST_MODEL_SERVICE`` names a model service,
and the gateway-skills checks only when ``AGENT_FOUNDRY_TEST_GATEWAY_SKILL``
names a skill.
"""

from __future__ import annotations

from typing import Any

import pytest
from openai import AsyncOpenAI

from agent_foundry import env
from agent_foundry.testing.live import live_required
from agent_foundry.testing.workspace import WorkspaceConfigError, client_for_binding, load_workspace_binding


@pytest.fixture(scope="session")
def serving_ws() -> Any:
    """Workspace client from env/profile auth. No generated project required."""
    try:
        return client_for_binding(load_workspace_binding())
    except WorkspaceConfigError as exc:
        if live_required():
            pytest.fail(str(exc))
        pytest.skip(str(exc))


def _openai_client(ws: Any, path: str) -> AsyncOpenAI:
    token = ws.config.authenticate()["Authorization"].removeprefix("Bearer ")
    return AsyncOpenAI(api_key=token, base_url=f"{ws.config.host.rstrip('/')}/{path}")


@pytest.fixture
def serving_client(serving_ws: Any) -> AsyncOpenAI:
    """Function-scoped: the async HTTP client must not outlive one test's event loop."""
    return _openai_client(serving_ws, "serving-endpoints")


@pytest.fixture(scope="session")
def model_service() -> str:
    """The Unity Gateway model service FQN; skips when not configured."""
    name = (env.read_optional(env.AGENT_FOUNDRY_TEST_MODEL_SERVICE) or "").strip()
    if not name:
        pytest.skip(f"set {env.AGENT_FOUNDRY_TEST_MODEL_SERVICE.name} to run the Unity Gateway model-service variants")
    return name


@pytest.fixture(scope="session")
def gateway_skill() -> str:
    """A Unity Catalog skill FQN readable by the test principal; skips when not configured."""
    name = (env.read_optional(env.AGENT_FOUNDRY_TEST_GATEWAY_SKILL) or "").strip()
    if not name:
        pytest.skip(f"set {env.AGENT_FOUNDRY_TEST_GATEWAY_SKILL.name} to run the gateway-skills live checks")
    return name


@pytest.fixture
def gateway_client(serving_ws: Any) -> AsyncOpenAI:
    """Same as :func:`serving_client`, on the Unity Gateway unified chat API."""
    return _openai_client(serving_ws, "ai-gateway/mlflow/v1")
