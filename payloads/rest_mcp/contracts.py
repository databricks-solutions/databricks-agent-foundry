"""Typed contract for the ``rest_mcp`` payload's ``spec`` in ``payload.yml``.

This is **payload-owned**: the agent-foundry framework knows only the
generic payload envelope (:class:`agent_foundry.contracts.payload.PayloadFile`
-- ``adapter`` + an opaque ``spec``).  The ``rest_mcp`` payload defines what
its ``spec`` means here, so the framework never has to know what a
"REST->MCP proxy" is.

The authoring surface lives in one file,
``instances/<inst>/payloads/rest_mcp/payload.yml``::

    payload:
      adapter: rest_mcp
      spec:
        apis:                       # each entry -> its own MCP mount + UC connection/service
          - name: orders
            rest:                   # hop 2: app -> upstream REST API
              base_url: "https://orders.internal.example.com"
              allowed_hosts: ["orders.internal.example.com"]
              auth: { mode: static, secret_scope: orders_api, secret_key: bearer_token }
            tools:                  # each tool -> one MCP method on this server
              - name: get_order_status
                description: "Fetch the current status of an order by id."
                request:
                  method: GET
                  path: "/orders/{order_id}/status"
                  args:
                    - { name: order_id, in: path, type: string, required: true }
                response:
                  project: ["status", "updated_at"]

The guiding principle -- how the mapping stays consistent *and* generic:

    One tool declaration is the single source of truth for three derived
    artifacts that must never drift: (a) the MCP tool ``inputSchema`` on the
    MCP card, (b) the ``httpx`` request the engine builds, and (c) the
    response projection.  All three are generated from the same ``args`` list,
    so the card can never lie about what the call actually does.

Best practices baked into the contract:

* **Strict ``extra="forbid"``** on every model -- a typo'd key fails fast.
* **Fail-closed:** non-GET requires ``mutating: true``; ``allowed_hosts`` +
  https-only egress; ``{{arg}}`` template placeholders must resolve against
  declared args / ``input_schema``; ``code_tools`` module paths are confined
  to the instance tree (no absolute paths, no ``..`` traversal).
* **Auth as a discriminated union on ``mode``** (``none | static | obo``) --
  extensible without breaking existing configs.  Secrets are referenced by
  scope/key only, never inlined.  ``obo`` is advanced (Databricks-ecosystem
  targets only); default to ``static`` / ``none``.

Only hop 2 (``rest.auth``, app->API) is modelled here.  Hop 1 (agent->app) is
the UC connection / MCP Service provisioned at deploy time and referenced by
the paired ``mcp_servers/<api>/server.yml`` -- which is why the proxy needs
no agent-runtime changes.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent_foundry.contracts.payload import PayloadBrickRequirement

# ``extra="forbid"`` on every spec model: a misplaced or misspelled key is a
# contract error and must fail fast (hard-cut 2.0 surface, no legacy fallback).
_SPEC_CONFIG = ConfigDict(extra="forbid")
# ``ToolArg`` also accepts the field's Python name in addition to its ``in``
# alias, so the model round-trips in code as well as from YAML.
_ALIASED_CONFIG = ConfigDict(extra="forbid", populate_by_name=True)

# Matches ``{{ arg }}`` placeholders in a Tier-2 ``body_template`` -- variable
# substitution only (no conditionals / loops), so the template stays a
# deterministic single-source-of-truth mapping.
_TEMPLATE_PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")
# Matches ``{arg}`` placeholders in a request ``path``.
_PATH_PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

ArgType = Literal["string", "integer", "number", "boolean", "array", "object"]
HttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]


# ---------------------------------------------------------------------------
# hop-2 auth (app -> upstream REST API) -- discriminated union on ``mode``
# ---------------------------------------------------------------------------


class NoAuth(BaseModel):
    """``auth: { mode: none }`` -- unauthenticated egress (public APIs)."""

    model_config = _SPEC_CONFIG

    mode: Literal["none"] = "none"


class StaticAuth(BaseModel):
    """``auth: { mode: static }`` -- a bearer token from a Databricks secret.

    The token is referenced by scope/key only; it is never inlined in the
    config and never logged.
    """

    model_config = _SPEC_CONFIG

    mode: Literal["static"]
    secret_scope: str
    secret_key: str
    # Header the token is sent in and the value prefix, e.g.
    # ``Authorization: Bearer <token>``.  Overridable for APIs that use a
    # bespoke header (``X-Api-Key`` with no prefix, ...).
    header: str = "Authorization"
    value_prefix: str = "Bearer "


class OboAuth(BaseModel):
    """``auth: { mode: obo }`` -- forward the caller's identity (advanced).

    On-Behalf-Of forwards the caller's ``x-forwarded-access-token``.  It only
    reaches **Databricks-ecosystem** resources (no dynamic client registration,
    U2M won't auto-prompt for arbitrary third-party APIs), so it is gated as
    advanced -- default to ``static`` / ``none`` for non-Databricks targets.
    """

    model_config = _SPEC_CONFIG

    mode: Literal["obo"]


RestAuth = Annotated[
    NoAuth | StaticAuth | OboAuth,
    Field(discriminator="mode"),
]


# ---------------------------------------------------------------------------
# rest (per-API upstream connection config)
# ---------------------------------------------------------------------------


class RestDefaults(BaseModel):
    """``rest.defaults`` -- per-API request defaults (overridable per tool later)."""

    model_config = _SPEC_CONFIG

    timeout_s: int = Field(default=30, gt=0)
    # Hard cap on the bytes returned to the LLM -- keeps context lean and
    # bounds a hostile / runaway upstream response.
    max_response_bytes: int = Field(default=100_000, gt=0)


class RestConfig(BaseModel):
    """``api.rest`` -- how the app reaches one upstream REST API (hop 2)."""

    model_config = _SPEC_CONFIG

    base_url: str
    # Egress allow-list: the engine refuses any request whose host is not
    # listed.  ``base_url``'s host must appear here.
    allowed_hosts: list[str] = Field(min_length=1)
    auth: RestAuth = NoAuth()
    defaults: RestDefaults = RestDefaults()

    @field_validator("base_url")
    @classmethod
    def _require_https(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme != "https":
            raise ValueError(f"rest.base_url must be https:// (got {value!r})")
        if not parsed.netloc:
            raise ValueError(f"rest.base_url must include a host (got {value!r})")
        return value

    @model_validator(mode="after")
    def _base_host_in_allowlist(self) -> RestConfig:
        host = urlparse(self.base_url).hostname
        if host is not None and host not in self.allowed_hosts:
            raise ValueError(f"rest.base_url host {host!r} must be listed in rest.allowed_hosts {self.allowed_hosts!r}")
        return self


# ---------------------------------------------------------------------------
# tools (declarative -- Tier 1 flat args, Tier 2 templated body)
# ---------------------------------------------------------------------------


class ToolArg(BaseModel):
    """One declared tool input -- feeds the MCP ``inputSchema`` and the request.

    ``in`` fixes where the value lands: ``path`` (a ``{name}`` path segment),
    ``query`` (a query-string param), or ``body`` (a JSON body field, or a
    ``{{name}}`` substitution when a ``body_template`` is present).
    """

    model_config = _ALIASED_CONFIG

    name: str
    location: Literal["path", "query", "body"] = Field(alias="in")
    type: ArgType = "string"
    required: bool = False
    description: str | None = None


class ToolRequest(BaseModel):
    """``tool.request`` -- the upstream call the tool maps to."""

    model_config = _SPEC_CONFIG

    method: HttpMethod
    path: str
    args: list[ToolArg] = []
    # Tier 2: a JSON body with ``{{arg}}`` placeholders substituted from the
    # declared args.  Non-GET only.  Placeholders are validated against the
    # arg names (+ ``input_schema`` top-level properties) at load time.
    body_template: str | None = None
    # Optional explicit MCP ``inputSchema`` (verbatim JSON Schema) for shaped
    # bodies; when omitted the engine derives the schema from ``args``.
    input_schema: dict[str, Any] | None = None

    @field_validator("path")
    @classmethod
    def _path_is_absolute(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError(f"request.path must start with '/' (got {value!r})")
        return value

    @model_validator(mode="after")
    def _validate_shape(self) -> ToolRequest:
        self._reject_duplicate_args()
        self._validate_path_args()
        self._validate_body_template()
        return self

    def _reject_duplicate_args(self) -> None:
        names = [a.name for a in self.args]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"request.args has duplicate names: {dupes}")

    def _validate_path_args(self) -> None:
        declared_path = {a.name for a in self.args if a.location == "path"}
        referenced = set(_PATH_PLACEHOLDER.findall(self.path))
        missing = sorted(declared_path - referenced)
        if missing:
            raise ValueError(f"path args {missing} are not referenced in request.path {self.path!r}")
        undeclared = sorted(referenced - declared_path)
        if undeclared:
            raise ValueError(f"request.path {self.path!r} references {undeclared} not declared as path args (in: path)")

    def _validate_body_template(self) -> None:
        if self.body_template is None:
            return
        if self.method == "GET":
            raise ValueError("request.body_template is not allowed on GET requests")
        known = {a.name for a in self.args} | set((self.input_schema or {}).get("properties", {}))
        placeholders = set(_TEMPLATE_PLACEHOLDER.findall(self.body_template))
        unknown = sorted(placeholders - known)
        if unknown:
            raise ValueError(
                f"request.body_template references {unknown} which are not declared args or input_schema properties"
            )


class ToolResponse(BaseModel):
    """``tool.response`` -- how the upstream response is projected for the LLM."""

    model_config = _SPEC_CONFIG

    # Whitelist of top-level JSON keys returned to the caller; omit to return
    # the whole (byte-capped) body.
    project: list[str] | None = None


class ToolSpec(BaseModel):
    """One declarative tool -> one MCP method on this API's server."""

    model_config = _SPEC_CONFIG

    name: str
    description: str
    # Non-GET is opt-in: a mutating call must say so, or it fails closed.
    mutating: bool = False
    request: ToolRequest
    response: ToolResponse = ToolResponse()

    @model_validator(mode="after")
    def _mutating_matches_method(self) -> ToolSpec:
        is_get = self.request.method == "GET"
        if not is_get and not self.mutating:
            raise ValueError(f"tool {self.name!r}: {self.request.method} is mutating and requires 'mutating: true'")
        if is_get and self.mutating:
            raise ValueError(f"tool {self.name!r}: GET requests cannot set 'mutating: true'")
        return self


class CodeToolSpec(BaseModel):
    """A Tier-3 code tool -- an instance-side Python module implementing the
    payload's tool contract, for shapes needing computation.

    The ``module`` path is instance-relative and synced into the app; it is
    confined to the instance tree (no absolute paths, no ``..`` traversal) so
    a code tool can never point outside the deployed instance.
    """

    model_config = _SPEC_CONFIG

    module: str

    @field_validator("module")
    @classmethod
    def _confined_relative_path(cls, value: str) -> str:
        if not value.endswith(".py"):
            raise ValueError(f"code_tools.module must be a .py file (got {value!r})")
        if value.startswith("/") or value.startswith("~"):
            raise ValueError(f"code_tools.module must be instance-relative (got {value!r})")
        parts = value.replace("\\", "/").split("/")
        if ".." in parts:
            raise ValueError(f"code_tools.module must not traverse parents (got {value!r})")
        return value


# ---------------------------------------------------------------------------
# MCP Service (optional governance layer -- path C, Beta; see payload README)
# ---------------------------------------------------------------------------
#
# This block is purely additive on top of the ``is_mcp`` connection and is
# **opt-in per API** (omit the ``mcp_service`` field -> connection-only, as
# today).  All field shapes below were live-confirmed against the Beta
# ``/api/2.1/unity-catalog/mcp-services`` API; the friendly config values are
# mapped to the platform's ``RATE_LIMIT_*`` / ``POLICY_TYPE_*`` enum tokens in
# :mod:`payloads.rest_mcp.deploy.mcp_service`, so config churn stays out of the
# user-facing surface.

# Rate-limit scope. The platform only supports per-principal limits for MCP
# (``endpoint``-wide is rejected), so ``principal`` is always required.
McpRateLimitKey = Literal["user", "user_group", "service_principal"]
# Service-policy handler kind: a platform built-in policy name, or a custom UC
# function (``catalog.schema.function``).
McpPolicyType = Literal["builtin", "custom"]


class RateLimitSpec(BaseModel):
    """One AI-Gateway rate limit on the MCP Service (per principal, per minute).

    Spike-confirmed: the MCP-services request body uses per-principal keys and a
    ``minute`` renewal window only.  ``calls`` is the request budget per window;
    it maps to the wire field the API actually reads for MCP (``tokens``) -- the
    serving-endpoint ``calls`` field is silently ignored there.
    """

    model_config = _SPEC_CONFIG

    key: McpRateLimitKey
    # User email, group name, or service-principal application id to limit.
    principal: str = Field(min_length=1)
    calls: int = Field(gt=0)
    renewal_period: Literal["minute"] = "minute"


class ServicePolicySpec(BaseModel):
    """One governance policy applied to the MCP Service (flat entry).

    ``builtin`` handlers are platform-defined policy names (validated by the
    platform); ``custom`` handlers are UC functions named
    ``catalog.schema.function`` (the deploy module wraps them as the API's
    ``functions/{fqn}`` handler form).
    """

    model_config = _SPEC_CONFIG

    name: str = Field(min_length=1)
    policy_type: McpPolicyType
    handler: str = Field(min_length=1)
    options: dict[str, str] = {}

    @model_validator(mode="after")
    def _validate_custom_handler(self) -> ServicePolicySpec:
        if self.policy_type == "custom":
            parts = self.handler.split(".")
            if len(parts) != 3 or not all(parts):
                raise ValueError(
                    f"service_policy {self.name!r}: a custom handler must be a 3-level UC function "
                    f"name 'catalog.schema.function' (got {self.handler!r})"
                )
        return self


class McpGrants(BaseModel):
    """Unity Catalog grants applied to the MCP Service securable."""

    model_config = _SPEC_CONFIG

    # Principals granted ``EXECUTE`` (invoke) on the service.
    execute: list[str] = []


class McpServiceSpec(BaseModel):
    """Optional path-C governance layer for one API (Beta; opt-in).

    Presence of this block opts the API's ``is_mcp`` connection into a UC **MCP
    Service** securable (``catalog.schema.<api>``) built *on top of* it.  Agent
    consumption stays on the reliable ``type: external`` connection; the service
    provisions the governance surface (tool selection, service policies, rate
    limits, EXECUTE grants) that activates when consumption later moves to the AI
    Gateway URL.  ``catalog`` / ``schema`` default to the instance's target
    catalog + ``<schema_prefix>_rest_mcp`` schema when omitted.
    """

    model_config = _ALIASED_CONFIG

    # Block-presence is the opt-in; ``enabled: false`` keeps the config but
    # skips provisioning (a clean off-switch without deleting the block).
    enabled: bool = True
    catalog: str | None = None
    uc_schema: str | None = Field(default=None, alias="schema")
    # Empty selector list = expose all of the API's tools.  A trailing ``*`` is a
    # prefix match (``get_*``); otherwise the entry is an exact tool name.
    include_tool_selectors: list[str] = []
    service_policies: list[ServicePolicySpec] = []
    rate_limits: list[RateLimitSpec] = []
    grants: McpGrants = McpGrants()

    @field_validator("include_tool_selectors")
    @classmethod
    def _validate_selectors(cls, values: list[str]) -> list[str]:
        for sel in values:
            if not sel:
                raise ValueError("include_tool_selectors entries must be non-empty")
            if sel.count("*") > 1 or ("*" in sel and not sel.endswith("*")):
                raise ValueError(f"tool selector {sel!r}: '*' is only supported as a single trailing prefix wildcard")
        return values


# ---------------------------------------------------------------------------
# api + spec + envelope
# ---------------------------------------------------------------------------


class ApiSpec(BaseModel):
    """One upstream REST API -> its own MCP mount + UC connection / service."""

    model_config = _SPEC_CONFIG

    # Identifier for the mount (``/mcp/<name>``) and the derived UC connection /
    # MCP Service name; must be a safe slug.
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    rest: RestConfig
    tools: list[ToolSpec] = []
    code_tools: list[CodeToolSpec] = []
    # Optional path-C governance layer (Beta). Omit for connection-only (path B).
    mcp_service: McpServiceSpec | None = None

    @model_validator(mode="after")
    def _has_tools_and_unique_names(self) -> ApiSpec:
        if not self.tools and not self.code_tools:
            raise ValueError(f"api {self.name!r} must declare at least one tool or code_tool")
        names = [t.name for t in self.tools]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"api {self.name!r} has duplicate tool names: {dupes}")
        return self


class RestMcpPayloadSpec(BaseModel):
    """``payload.spec`` for the ``rest_mcp`` adapter (the full authoring body)."""

    model_config = _SPEC_CONFIG

    apis: list[ApiSpec] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_api_names(self) -> RestMcpPayloadSpec:
        names = [a.name for a in self.apis]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"spec.apis has duplicate names: {dupes}")
        return self


class RestMcpPayloadConfig(BaseModel):
    """The ``payload:`` block when ``adapter: rest_mcp`` (typed spec)."""

    model_config = _SPEC_CONFIG

    adapter: str
    requires: list[PayloadBrickRequirement] = Field(default_factory=list)
    spec: RestMcpPayloadSpec


class RestMcpPayloadFile(BaseModel):
    """Top-level shape of the ``rest_mcp`` ``payload.yml`` (drives the schema)."""

    model_config = _SPEC_CONFIG

    payload: RestMcpPayloadConfig


__all__ = [
    "ApiSpec",
    "ArgType",
    "CodeToolSpec",
    "HttpMethod",
    "McpGrants",
    "McpPolicyType",
    "McpRateLimitKey",
    "McpServiceSpec",
    "NoAuth",
    "OboAuth",
    "RateLimitSpec",
    "RestAuth",
    "RestConfig",
    "RestDefaults",
    "RestMcpPayloadConfig",
    "RestMcpPayloadFile",
    "RestMcpPayloadSpec",
    "ServicePolicySpec",
    "StaticAuth",
    "ToolArg",
    "ToolRequest",
    "ToolResponse",
    "ToolSpec",
]
