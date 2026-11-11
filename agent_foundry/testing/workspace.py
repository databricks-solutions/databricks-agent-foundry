"""Runtime workspace binding for acceptance (env/CI, never example metadata)."""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, field_validator

from agent_foundry import env

_HOST_ENV = env.DATABRICKS_HOST.name
_PROFILE_ENV = env.DATABRICKS_CONFIG_PROFILE.name
_TOKEN_ENV = env.DATABRICKS_TOKEN.name
_CLIENT_ID_ENV = env.DATABRICKS_CLIENT_ID.name
_CLIENT_SECRET_ENV = env.DATABRICKS_CLIENT_SECRET.name
_AUTH_TYPE_ENV = env.DATABRICKS_AUTH_TYPE.name
_GITHUB_OIDC = "github-oidc"


class WorkspaceConfigError(ValueError):
    """Missing or inconsistent workspace configuration."""


class WorkspaceBinding(BaseModel):
    """Workspace values the harness feeds to ``agent-foundry init`` answers."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    host: str
    catalog: str
    target: str = "dev"
    profile: str | None = None

    @field_validator("host")
    @classmethod
    def _https_host(cls, value: str) -> str:
        parsed = urlparse(value.strip())
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError(f"DATABRICKS_HOST must be an https URL, got {value!r}")
        return f"{parsed.scheme}://{parsed.netloc}"

    @field_validator("catalog", "target")
    @classmethod
    def _nonempty(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must be non-empty")
        return stripped


def apply_acceptance_auth_env() -> None:
    """Leave exactly one Databricks SDK auth method in the environment.

    Empty ``DATABRICKS_TOKEN`` / client fields still count as set for the
    SDK and collide with OAuth M2M. If both a PAT and client-id/secret
    are present, drop the PAT so CI can keep a leftover token secret.
    """
    for name in (_TOKEN_ENV, _CLIENT_ID_ENV, _CLIENT_SECRET_ENV, _PROFILE_ENV):
        if not os.environ.get(name, "").strip():
            os.environ.pop(name, None)
    has_m2m = bool(os.environ.get(_CLIENT_ID_ENV) and os.environ.get(_CLIENT_SECRET_ENV))
    if has_m2m and os.environ.get(_TOKEN_ENV):
        os.environ.pop(_TOKEN_ENV, None)


def load_workspace_binding() -> WorkspaceBinding:
    """Read host/catalog from the environment.

    Auth is the Databricks SDK contract (GitHub OIDC federation, OAuth
    M2M, PAT, or named profile). This function only checks that *some*
    method is present;
    it does not construct a client.
    """
    apply_acceptance_auth_env()
    host = os.environ.get(_HOST_ENV, "").strip()
    catalog = os.environ.get(env.AGENT_FOUNDRY_TEST_CATALOG.name, "").strip()
    if not host:
        raise WorkspaceConfigError(f"{_HOST_ENV} is required for acceptance")
    if not catalog:
        raise WorkspaceConfigError(f"{env.AGENT_FOUNDRY_TEST_CATALOG.name} is required for acceptance")
    _require_auth()
    profile = os.environ.get(_PROFILE_ENV, "").strip() or None
    target = os.environ.get(env.AGENT_FOUNDRY_TARGET.name, "dev").strip() or "dev"
    return WorkspaceBinding(host=host, catalog=catalog, target=target, profile=profile)


def assert_host_matches_sdk(ws: Any, binding: WorkspaceBinding) -> None:
    """Refuse to deploy if the SDK client resolved a different workspace."""
    sdk_host = getattr(getattr(ws, "config", None), "host", None) or ""
    parsed = urlparse(str(sdk_host).strip())
    actual = f"{parsed.scheme}://{parsed.netloc}" if parsed.netloc else str(sdk_host).rstrip("/")
    if actual != binding.host:
        raise WorkspaceConfigError(
            f"SDK host {actual!r} does not match configured {binding.host!r}; refusing cross-workspace deploy"
        )


def _require_auth() -> None:
    if os.environ.get(_PROFILE_ENV, "").strip():
        return
    if os.environ.get(_TOKEN_ENV, "").strip():
        return
    client_id = os.environ.get(_CLIENT_ID_ENV, "").strip()
    if client_id and os.environ.get(_CLIENT_SECRET_ENV, "").strip():
        return
    if client_id and os.environ.get(_AUTH_TYPE_ENV, "").strip() == _GITHUB_OIDC:
        return
    raise WorkspaceConfigError(
        "acceptance needs DATABRICKS_CONFIG_PROFILE, DATABRICKS_TOKEN, "
        "DATABRICKS_CLIENT_ID + DATABRICKS_CLIENT_SECRET, "
        "or DATABRICKS_AUTH_TYPE=github-oidc + DATABRICKS_CLIENT_ID"
    )


def workspace_api_url(binding: WorkspaceBinding, path: str) -> str:
    """Absolute workspace URL. Never use ``WorkspaceClient.config.host`` here.

    The SDK often stores a scheme-less host; httpx then raises
    ``UnsupportedProtocol``.
    """
    suffix = path if path.startswith("/") else f"/{path}"
    return f"{binding.host.rstrip('/')}{suffix}"


def client_for_binding(binding: WorkspaceBinding) -> Any:
    """WorkspaceClient for a loaded acceptance binding (host, optional profile)."""
    from agent_foundry.workspace_client import (
        SKIP_DEFAULT_WORKSPACE_CLIENT_ENV,
        get_workspace_client,
    )

    os.environ.pop(SKIP_DEFAULT_WORKSPACE_CLIENT_ENV, None)
    apply_acceptance_auth_env()
    variables: dict[str, str] = {"host": binding.host}
    if binding.profile:
        variables["profile"] = binding.profile
    ws = get_workspace_client(variables, profile=binding.profile)
    assert_host_matches_sdk(ws, binding)
    return ws


__all__ = [
    "WorkspaceBinding",
    "WorkspaceConfigError",
    "apply_acceptance_auth_env",
    "assert_host_matches_sdk",
    "client_for_binding",
    "load_workspace_binding",
    "workspace_api_url",
]
