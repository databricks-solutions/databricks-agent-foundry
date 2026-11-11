"""Pin the Databricks SDK surface the Unity Gateway integration relies on.

An SDK upgrade that renames or drops one of these fails here instead of
at deploy or request time.
"""

from __future__ import annotations

import inspect
from importlib.metadata import version
from typing import get_type_hints

import pytest
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.catalog import AiGatewayAPI, Privilege, SecurableType

_MIN_SDK = (0, 151, 0)


def test_installed_sdk_meets_the_floor() -> None:
    installed = tuple(int(part) for part in version("databricks-sdk").split(".")[:3])
    assert installed >= _MIN_SDK


def test_workspace_client_exposes_ai_gateway() -> None:
    accessor = inspect.getattr_static(WorkspaceClient, "ai_gateway")
    assert isinstance(accessor, property)
    assert accessor.fget is not None
    assert get_type_hints(accessor.fget)["return"] is AiGatewayAPI


@pytest.mark.parametrize(
    "method",
    ["list_mcp_services", "get_mcp_service", "list_skills", "get_skill", "create_skill", "finalize_skill"],
)
def test_ai_gateway_api_methods(method: str) -> None:
    assert callable(getattr(AiGatewayAPI, method, None))


def test_grant_enums_cover_gateway_securables() -> None:
    assert {"MCP_SERVICE", "SKILL", "MODEL_SERVICE"} <= {member.name for member in SecurableType}
    assert {"EXECUTE", "READ_VOLUME"} <= {member.name for member in Privilege}
