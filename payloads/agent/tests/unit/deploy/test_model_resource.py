"""Tests for the App resource that grants the agent's model.

A serving endpoint keeps the brick-default ``serving_endpoint`` resource;
a Unity Gateway model service swaps it for a ``MODEL_SERVICE`` securable.
The App must never carry both.
"""

from __future__ import annotations

from typing import Any

from payloads.agent.contracts import AgentPayloadSpec
from payloads.agent.deploy.config import model_app_resources

_FQN = "system.ai.claude-sonnet-4-5"


def _brick_defaults(endpoint_name: str) -> list[dict[str, Any]]:
    """The substituted brick-default resources that matter here."""
    return [
        {"name": "llm-endpoint", "serving_endpoint": {"name": endpoint_name, "permission": "CAN_QUERY"}},
        {"name": "sql-warehouse", "sql_warehouse": {"id": "wh", "permission": "CAN_USE"}},
    ]


def _spec(runtime: dict[str, Any]) -> AgentPayloadSpec:
    return AgentPayloadSpec.model_validate({"runtime": runtime})


def test_serving_endpoint_keeps_can_query_resource() -> None:
    raw = _brick_defaults("databricks-claude-sonnet-4-5")
    spec = _spec({"model": {"kind": "serving_endpoint", "name": "databricks-claude-sonnet-4-5"}})
    assert model_app_resources(raw, spec) == raw


def test_model_service_replaces_endpoint_with_securable() -> None:
    raw = _brick_defaults(_FQN)
    resources = model_app_resources(raw, _spec({"model": {"kind": "model_service", "name": _FQN}}))

    assert not any("serving_endpoint" in r for r in resources)
    assert {"name": "sql-warehouse", "sql_warehouse": {"id": "wh", "permission": "CAN_USE"}} in resources
    assert resources[-1] == {
        "name": "llm-model-service",
        "uc_securable": {
            "securable_full_name": _FQN,
            "securable_type": "MODEL_SERVICE",
            "permission": "EXECUTE",
        },
    }


def test_model_service_keeps_unrelated_serving_endpoints() -> None:
    raw = [
        *_brick_defaults(_FQN),
        {"name": "reranker", "serving_endpoint": {"name": "rerank", "permission": "CAN_QUERY"}},
    ]
    resources = model_app_resources(raw, _spec({"model": {"kind": "model_service", "name": _FQN}}))
    endpoint_names = [r["serving_endpoint"]["name"] for r in resources if "serving_endpoint" in r]
    assert endpoint_names == ["rerank"]
