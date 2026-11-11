"""Wheel-shipped helpers for downstream and framework acceptance tests.

This package is the testing contract. It does **not** extend
``agent_foundry.init.examples_registry``: init still only discovers
starter variants, while this module owns scenario lanes, workspace
bindings, readiness polling, and response contracts.
"""

from __future__ import annotations

from agent_foundry.testing.identity import RunIdentity, make_run_identity
from agent_foundry.testing.responses import (
    BasicInvocationContract,
    ResponseFormatContract,
    extract_function_call_outputs,
    extract_function_calls,
    extract_message_items,
    extract_text,
)
from agent_foundry.testing.scenarios import (
    Capability,
    Lane,
    Scenario,
    ScenarioCatalogError,
    load_scenarios,
)
from agent_foundry.testing.workspace import (
    WorkspaceBinding,
    WorkspaceConfigError,
    assert_host_matches_sdk,
    load_workspace_binding,
)

__all__ = [
    "BasicInvocationContract",
    "Capability",
    "Lane",
    "ResponseFormatContract",
    "RunIdentity",
    "Scenario",
    "ScenarioCatalogError",
    "WorkspaceBinding",
    "WorkspaceConfigError",
    "assert_host_matches_sdk",
    "extract_function_call_outputs",
    "extract_function_calls",
    "extract_message_items",
    "extract_text",
    "load_scenarios",
    "load_workspace_binding",
    "make_run_identity",
]
