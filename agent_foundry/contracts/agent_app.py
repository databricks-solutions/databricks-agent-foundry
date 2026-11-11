"""Typed contract for ``instances/<inst>/app/config.yml``.

This model mirrors the **raw YAML shape** of the Agent App brick config.
Historically this was the only untyped config surface in the framework:
the file was shallow-merged on top of the brick defaults at deploy time
with no schema gating the keys.  Typing it makes the deploy contract
deterministic -- a typo or an invalid ``compute_size`` fails fast instead
of silently deploying the wrong app size.

Unlike the lax sibling YAML contracts (``AgentYamlConfig`` /
``WarehouseConfig`` use ``extra="ignore"``), this model uses
``extra="forbid"``: unknown top-level keys are rejected so misspelled
keys cannot slip through.  ``forbid`` applies only at the top level, so
the flexible nested resource dicts (``app_resources``) stay open.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

# Databricks Apps OBO (on-behalf-of) user authorization scopes.  This is
# the closed set the Apps platform accepts for ``user_api_scopes``; an
# unknown scope is rejected by the API at deploy time, so we mirror it
# here to fail fast at config-load instead.  Keep in sync with the
# Databricks Apps "User authorization" scope catalogue.
UserApiScope = Literal[
    "sql",
    "dashboards.genie",
    "serving.serving-endpoints",
    "vectorsearch.vector-search-indexes",
    "catalog.catalogs:read",
    "catalog.schemas:read",
    "catalog.tables:read",
    "mcp.functions",
    "mcp.vectorsearch",
    "mcp.genie",
    "mcp.external",
    "genie",
    "model-serving",
    "postgres",
    "catalog.connections",
    "files",
    "ai-gateway",
    "vector-search",
    "workspace.workspace",
]


class AgentAppConfig(BaseModel):
    """The YAML shape of ``app/config.yml``.

    ``compute_size`` maps to the Databricks Apps ``ComputeSize`` enum
    (``MEDIUM`` / ``LARGE``); the deploy script uppercases it before
    passing it to the SDK.

    ``user_authorization`` is a closed enum of OBO scopes
    (:data:`UserApiScope`) so a misspelled scope fails the deploy
    instead of being silently sent to (and rejected by) the API.
    """

    model_config = ConfigDict(extra="forbid")

    compute_size: Literal["medium", "large"] = "medium"
    # Host's declaration of which payload(s) to mount in the App
    # container (issue #338).  ``app/`` is a generic host; each named
    # payload owns its runtime surface under ``payloads/<name>/`` (config,
    # skills, MCP servers, and the ``payload.yml`` adapter + capabilities
    # that are the single source for runtime toggles).  Defaults to the
    # bundled ``agent`` payload.
    payloads: list[str] = ["agent"]
    app_resources: list[dict[str, Any]] = []
    sp_grants: list[dict[str, Any]] = []
    user_authorization: list[UserApiScope] = []
    # App telemetry export target, ``catalog.schema`` or
    # ``catalog.schema.prefix`` (tables ``<prefix>_otel_logs|metrics|spans``).
    # ``None`` leaves the App's telemetry setting untouched.
    telemetry_destination: str | None = None
