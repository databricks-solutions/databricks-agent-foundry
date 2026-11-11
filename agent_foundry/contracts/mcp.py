"""Typed contracts for ``instances/<inst>/mcp_servers/<name>/server.yml``.

These Pydantic models mirror the user-facing YAML shape consumed by the
:class:`payloads.agent.deploy.mcp.brick.MCPBrick`.

Six server types are modelled as a discriminated union on the ``type``
field so deploy / runtime code can narrow the concrete config via
``match`` statements without manual dispatch.

Scope note
----------
PR 1 (#107) ships the *deploy-time* contract only -- the runtime hook in
``payloads/agent/runtime/brick_mcp_servers.py:build_mcp_servers`` that consumes the brick state
lands in PR 2 (#108).
"""

from __future__ import annotations

import re
from typing import Annotated, Any, ClassVar, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ---------------------------------------------------------------------------
# Shared
# ---------------------------------------------------------------------------

# ``extra="ignore"`` matches the rest of ``agent_foundry.contracts``:
# unknown keys are silently dropped so YAMLs are forward-compatible.
# ``populate_by_name=True`` lets ``schema_`` accept both ``schema:`` (YAML)
# and ``schema_`` (Python kwarg) inputs -- needed because ``schema`` is a
# reserved attribute name on Pydantic ``BaseModel``.
_MODEL_CONFIG = ConfigDict(extra="ignore", populate_by_name=True)

_NAME_PREFIX_RE = re.compile(r"^[a-z][a-z0-9_]*__$")


# ---------------------------------------------------------------------------
# Primitive configs (tools / prompts / resources)
# ---------------------------------------------------------------------------


class ToolAllowEntry(BaseModel):
    """One allow-list entry that can glob-match upstream and rename.

    Two motivating cases (see issue tracker for the agent-foundry
    framework):

    - **Glob upstream names.** Databricks managed Genie servers expose
      ``query_space_<space_uuid>`` -- the UUID is impossible to put in
      ``tools.allow`` as a literal across deploys. ``match: "query_space_*"``
      lets the user write a stable allow-list.
    - **Rename to a clean alias.** Managed Functions / Vector Search
      servers expose fully-qualified names like
      ``<catalog>__<schema>__<func>``. Concatenated with the brick's
      ``name_prefix`` the result is unreadable for the LLM.
      ``expose_as: <short_name>`` collapses the upstream name to a
      stable alias before ``name_prefix`` is applied.

    Patterns use :mod:`fnmatch`-style globs (``*``, ``?``, ``[abc]``);
    matching is case-sensitive because MCP tool names are documented as
    case-sensitive identifiers. When ``expose_as`` is omitted the alias
    defaults to the upstream name, so a glob like ``"poll_*"`` keeps
    each matched name verbatim while still admitting them.
    """

    model_config = _MODEL_CONFIG

    match: str
    expose_as: str | None = None

    @field_validator("match")
    @classmethod
    def _match_non_empty(cls, value: str) -> str:
        if not value:
            raise ValueError(
                "tools.allow[].match must be a non-empty string (exact upstream name or fnmatch glob)",
            )
        return value

    @field_validator("expose_as")
    @classmethod
    def _expose_as_no_glob(cls, value: str | None) -> str | None:
        # Aliases must be literal identifiers because they end up as
        # the LLM-visible tool name; supporting globs here would let
        # one entry collide with itself across multiple matches.
        if value is None:
            return value
        if not value:
            raise ValueError(
                "tools.allow[].expose_as must be a non-empty string (omit the field to keep the upstream name)",
            )
        if any(ch in value for ch in "*?["):
            raise ValueError(
                f"tools.allow[].expose_as must be a literal name without glob metacharacters; got {value!r}",
            )
        return value


class ToolsConfig(BaseModel):
    """Tools surface for an MCP server.

    ``allow`` is the explicit allow-list of tool names exposed to the
    LLM. Each entry is either a bare string (exact match against the
    upstream name, no rename -- the historical shape) or a
    :class:`ToolAllowEntry` mapping with ``match`` (string or glob) and
    optional ``expose_as`` (alias). The brick fails closed when
    ``allow`` is empty.

    ``name_prefix`` is left as ``None`` here; the deploy script injects
    ``<server_name>__`` when unset so the model stays decoupled from
    on-disk directory naming.
    """

    model_config = _MODEL_CONFIG

    allow: list[Union[str, ToolAllowEntry]]
    name_prefix: str | None = None
    default_meta: dict[str, Any] = Field(default_factory=dict)

    @field_validator("allow")
    @classmethod
    def _allow_non_empty(
        cls,
        value: list[Union[str, ToolAllowEntry]],
    ) -> list[Union[str, ToolAllowEntry]]:
        if not value:
            raise ValueError("tools.allow must contain at least one tool name (MCP brick fails closed -- see #107)")
        return value

    @model_validator(mode="after")
    def _alias_uniqueness(self) -> "ToolsConfig":
        # The wrapper's reverse map (alias -> upstream) must be
        # injective; two entries collapsing to the same alias would
        # silently shadow one upstream call. Bare strings are aliased
        # to themselves, so they participate in the same uniqueness
        # check.
        aliases: list[str] = []
        for entry in self.allow:
            if isinstance(entry, ToolAllowEntry):
                aliases.append(entry.expose_as or entry.match)
            else:
                aliases.append(entry)
        seen: set[str] = set()
        dupes: list[str] = []
        for alias in aliases:
            if alias in seen:
                dupes.append(alias)
            seen.add(alias)
        if dupes:
            raise ValueError(
                "tools.allow has duplicate alias(es) "
                f"{sorted(set(dupes))!r}; each entry must collapse to a "
                "unique LLM-visible name (set distinct expose_as values)",
            )
        return self

    @field_validator("name_prefix")
    @classmethod
    def _name_prefix_pattern(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if not _NAME_PREFIX_RE.match(value):
            raise ValueError(
                f"tools.name_prefix must match {_NAME_PREFIX_RE.pattern!r} (lowercase, ends with '__'); got {value!r}"
            )
        return value


class PromptsConfig(BaseModel):
    """Server-side prompt consumption.

    Disabled by default ("discover but do not consume"). Setting
    ``enabled: true`` with an empty ``allow`` is legal but warned about
    by ``payloads/agent/deploy/mcp/validate.py`` -- usually a config mistake.

    ``cache: per_process`` is a forward-compat literal; PR 1 rejects it
    so callers fail fast rather than silently downgrading.
    """

    model_config = _MODEL_CONFIG

    enabled: bool = False
    allow: list[str] = Field(default_factory=list)
    inject_into: Literal[
        "dynamic_instructions",
        "system_prompt",
        "none",
    ] = "dynamic_instructions"
    cache: Literal[
        "per_request",
        "per_conversation",
        "per_process",
    ] = "per_conversation"

    @field_validator("cache")
    @classmethod
    def _per_process_unsupported(cls, value: str) -> str:
        if value == "per_process":
            raise NotImplementedError(
                "prompts.cache: per_process is forward-compat only and not implemented in v1 (see #106 non-goals)"
            )
        return value


class ResourcesConfig(BaseModel):
    """Server-side resource consumption (URI-keyed read-only data).

    PR 1 only models the shape; the ``read_tool`` exposure path lands in
    PR 4 (#110). ``allow`` accepts exact URIs *or* glob patterns -- glob
    is the natural fit for content-addressable servers.
    """

    model_config = _MODEL_CONFIG

    enabled: bool = False
    allow: list[str] = Field(default_factory=list)
    expose_as: Literal["read_tool", "none"] = "read_tool"


# ---------------------------------------------------------------------------
# Auto-poll: collapse async submit -> poll patterns into one transparent call
# ---------------------------------------------------------------------------


class ArgPassthrough(BaseModel):
    """One field-to-arg mapping in :class:`AutoPollConfig.forward_args`.

    The submit response is parsed as JSON; ``from_field`` is a dotted
    path inside that payload (``conversation_id``, ``status.state``);
    the looked-up value is passed to the poll tool under ``to_arg``.

    Two forwards are required for managed Genie (``conversation_id`` and
    ``message_id``); managed SQL needs only ``statement_id``.  Modelled
    as an explicit list rather than a flat dict so the deploy state JSON
    keeps a stable order and the alias rules are explicit.
    """

    model_config = _MODEL_CONFIG

    from_field: str
    to_arg: str

    @field_validator("from_field", "to_arg")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value:
            raise ValueError(
                "auto_poll.forward_args[].from_field/to_arg must be non-empty",
            )
        return value


class AutoPollConfig(BaseModel):
    """Wrapper-side polling for async ``submit -> poll`` tool patterns.

    Several Databricks-managed MCP servers expose a two-call shape: the
    submit tool returns a receipt with a status ``PENDING`` (or similar),
    and a poll tool re-fetches the same receipt until terminal.  When
    auto-poll is enabled, the wrapper absorbs the polling cycle: the
    LLM sees ONE call to the trigger tool and ONE response (the
    terminal poll result), with no intermediate "tool cells" cluttering
    the chat UI.

    Per-type defaults (:func:`ManagedSqlServerConfig._default_auto_poll`,
    :func:`ManagedGenieServerConfig._default_auto_poll`) ship out of the
    box; the YAML knob ``auto_poll: true`` selects the type default,
    ``auto_poll: false`` disables, and ``auto_poll: { ... }`` overrides
    fully.

    Notes:
    - When ``hide_poll_tool`` is True the poll tool is dropped from
      :meth:`list_tools` -- the LLM cannot call it directly.  The
      wrapper still needs ``poll_with`` in ``tools.allow`` to map the
      alias back to the upstream name internally.
    - On timeout the wrapper returns the LAST poll result it saw (still
      pending).  The upstream's own message is preserved so the LLM can
      explain the timeout to the user.
    """

    model_config = _MODEL_CONFIG

    trigger: str
    poll_with: str
    forward_args: list[ArgPassthrough]
    state_field: str = "status.state"
    pending_states: list[str] = Field(default_factory=lambda: ["PENDING"])
    timeout_s: float = Field(default=60.0, gt=0)
    initial_interval_s: float = Field(default=0.5, gt=0)
    max_interval_s: float = Field(default=5.0, gt=0)
    backoff_factor: float = Field(default=2.0, ge=1.0)
    hide_poll_tool: bool = True

    @field_validator("trigger", "poll_with")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value:
            raise ValueError(
                "auto_poll.trigger / auto_poll.poll_with must be non-empty (use the alias name, post expose_as)",
            )
        return value

    @model_validator(mode="after")
    def _trigger_distinct_from_poll(self) -> "AutoPollConfig":
        if self.trigger == self.poll_with:
            raise ValueError(
                "auto_poll.trigger and auto_poll.poll_with must differ "
                f"(both are {self.trigger!r}); the trigger is the submit "
                "tool, poll_with is the chaser",
            )
        return self

    @model_validator(mode="after")
    def _forward_args_non_empty(self) -> "AutoPollConfig":
        if not self.forward_args:
            raise ValueError(
                "auto_poll.forward_args must contain at least one entry "
                "(typically the receipt ID returned by the submit call)",
            )
        return self


# ---------------------------------------------------------------------------
# Per-type ServerConfig variants
# ---------------------------------------------------------------------------


class _ServerConfigBase(BaseModel):
    """Shared shape across all six server types.

    The ``auto_poll`` field accepts three shapes:

    - ``true`` (or omitted) -- use the type's default ``AutoPollConfig``
      via :attr:`_DEFAULT_AUTO_POLL`.  Types without a default
      (``external``, ``custom_app``, etc.) treat ``true`` as ``false``
      because there is no upstream contract to lean on.
    - ``false`` -- explicitly disabled; the LLM sees both the submit
      and poll tools and is responsible for chaining them.
    - ``AutoPollConfig`` -- explicit override; bypasses the type
      default entirely.

    The :func:`_resolve_auto_poll` validator collapses these shapes to
    ``AutoPollConfig | None`` after construction, so downstream code
    only ever sees the resolved value.
    """

    model_config = _MODEL_CONFIG

    # Subclasses may override to a non-None ``AutoPollConfig`` to opt
    # the type into out-of-the-box auto-polling.  ``ClassVar`` so
    # Pydantic treats it as configuration, not a model field.
    _DEFAULT_AUTO_POLL: ClassVar[AutoPollConfig | None] = None

    description: str = ""
    tools: ToolsConfig
    prompts: PromptsConfig = Field(default_factory=PromptsConfig)
    resources: ResourcesConfig = Field(default_factory=ResourcesConfig)
    # Accepts bool | AutoPollConfig | None; resolved to AutoPollConfig | None
    # by ``_resolve_auto_poll`` below.  Default ``True`` selects the type
    # default (see ``_DEFAULT_AUTO_POLL`` per subclass).
    auto_poll: Union[AutoPollConfig, bool, None] = True

    @model_validator(mode="after")
    def _resolve_auto_poll(self) -> "_ServerConfigBase":
        # Collapse the user-facing tri-state to the runtime two-state so
        # the wrapper / deploy / state code never has to re-decide.
        # ``object.__setattr__`` because Pydantic models are
        # frozen-by-convention; a normal assignment would re-trigger
        # validation and recurse.
        if self.auto_poll is False:
            object.__setattr__(self, "auto_poll", None)
            return self
        if self.auto_poll is True or self.auto_poll is None:
            object.__setattr__(self, "auto_poll", self._DEFAULT_AUTO_POLL)
            return self
        return self

    @property
    def resolved_auto_poll(self) -> AutoPollConfig | None:
        """Type-narrowed view of ``auto_poll`` after validation.

        The declared field type is ``AutoPollConfig | bool | None`` to
        accept the tri-state YAML knob (``true`` / ``false`` / dict).
        The :meth:`_resolve_auto_poll` ``model_validator`` then collapses
        ``True``/``None`` to the type's default and ``False`` to ``None``,
        so post-construction the value is always ``AutoPollConfig | None``.

        Downstream code (wrapper, deploy, state) reads this property
        instead of ``self.auto_poll`` to avoid spurious ``union-attr``
        narrowing errors at every access site.  The ``isinstance`` guard
        is defensive: if the validator somehow did not run, the property
        still returns the typed view rather than raising at attribute
        access.
        """
        val = self.auto_poll
        return val if isinstance(val, AutoPollConfig) else None


class ManagedFunctionsServerConfig(_ServerConfigBase):
    """``{host}/api/2.0/mcp/functions/{catalog}/{schema}`` -- UC functions."""

    type: Literal["managed_functions"] = "managed_functions"
    catalog: str
    schema_: str = Field(alias="schema")


# Defaults are reasoned-from-shape, not exhaustively tested against every
# upstream version: managed Genie + managed SQL APIs are stable, but if
# Databricks evolves the JSON envelope (rename ``status.state`` to
# ``status``, add new pending values, etc.) the wrapper logs a warning
# and falls back to returning the last seen result rather than spinning
# forever.  See ``bricks/agent/mcp_wrapper.py:_auto_poll_loop`` for the
# fail-soft behaviour.
_MANAGED_SQL_AUTO_POLL = AutoPollConfig(
    trigger="execute_sql_read_only",
    poll_with="poll_sql_result",
    forward_args=[ArgPassthrough(from_field="statement_id", to_arg="statement_id")],
    state_field="status.state",
    pending_states=["PENDING", "RUNNING"],
    timeout_s=120.0,
    initial_interval_s=0.5,
    max_interval_s=5.0,
)

# Genie's submit response carries BOTH the conversation and message
# IDs, and the poll tool requires both.  The response JSON uses
# camelCase field names (``conversationId`` / ``messageId``) but the
# poll tool's *parameter* names are snake_case
# (``conversation_id`` / ``message_id``) -- a real-world mismatch
# verified by inspecting the live managed Genie MCP server response.
# Timeout matches the legacy direct-SDK constant
# ``bricks/agent/tools.py:_GENIE_TIMEOUT`` (5 minutes) since multi-step
# Genie answers are routinely slow.
#
# ``pending_states`` enumerates every Genie status string we have
# observed in the wild.  Notably ``PENDING_WAREHOUSE`` is the *first*
# status returned for a fresh conversation -- omitting it would cause
# the framework to treat the very first poll as terminal and skip the
# entire poll cycle.  ``INITIALIZING`` is included defensively against
# the edge case where the conversation hasn't started yet.
_MANAGED_GENIE_AUTO_POLL = AutoPollConfig(
    trigger="query_space",
    poll_with="poll_response",
    forward_args=[
        ArgPassthrough(from_field="conversationId", to_arg="conversation_id"),
        ArgPassthrough(from_field="messageId", to_arg="message_id"),
    ],
    state_field="status",
    pending_states=[
        "INITIALIZING",
        "PENDING",
        "PENDING_WAREHOUSE",
        "ASKING_AI",
        "EXECUTING_QUERY",
        "IN_PROGRESS",
    ],
    timeout_s=300.0,
    initial_interval_s=1.0,
    max_interval_s=10.0,
)


class ManagedGenieServerConfig(_ServerConfigBase):
    """``{host}/api/2.0/mcp/genie/{space_id}`` -- Genie Spaces."""

    type: Literal["managed_genie"] = "managed_genie"
    space_id: str
    _DEFAULT_AUTO_POLL: ClassVar[AutoPollConfig | None] = _MANAGED_GENIE_AUTO_POLL


class ManagedVectorSearchServerConfig(_ServerConfigBase):
    """``{host}/api/2.0/mcp/vector-search/{catalog}/{schema}`` -- Vector Search."""

    type: Literal["managed_vector_search"] = "managed_vector_search"
    catalog: str
    schema_: str = Field(alias="schema")


class ManagedSqlServerConfig(_ServerConfigBase):
    """``{host}/api/2.0/mcp/sql`` -- Databricks SQL (no extra fields)."""

    type: Literal["managed_sql"] = "managed_sql"
    _DEFAULT_AUTO_POLL: ClassVar[AutoPollConfig | None] = _MANAGED_SQL_AUTO_POLL


class ExternalServerConfig(_ServerConfigBase):
    """``{host}/api/2.0/mcp/external/{connection_name}`` -- external proxied via a UC connection."""

    type: Literal["external"] = "external"
    connection_name: str


class CustomAppServerConfig(_ServerConfigBase):
    """User-hosted MCP server reached at ``{app_url}`` (full URL).

    Auth is OBO-only; the runtime hook enforces this when constructing the
    wrapped server.
    """

    type: Literal["custom_app"] = "custom_app"
    app_url: str


ServerConfig = Annotated[
    Union[
        ManagedFunctionsServerConfig,
        ManagedGenieServerConfig,
        ManagedVectorSearchServerConfig,
        ManagedSqlServerConfig,
        ExternalServerConfig,
        CustomAppServerConfig,
    ],
    Field(discriminator="type"),
]


__all__ = [
    "ArgPassthrough",
    "AutoPollConfig",
    "CustomAppServerConfig",
    "ExternalServerConfig",
    "ManagedFunctionsServerConfig",
    "ManagedGenieServerConfig",
    "ManagedSqlServerConfig",
    "ManagedVectorSearchServerConfig",
    "PromptsConfig",
    "ResourcesConfig",
    "ServerConfig",
    "ToolAllowEntry",
    "ToolsConfig",
]
