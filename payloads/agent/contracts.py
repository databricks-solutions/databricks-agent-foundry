"""Typed contract for the agent payload's ``spec`` in ``payload.yml``.

This is **payload-owned**: the agent-foundry framework knows only the
generic payload envelope (:class:`agent_foundry.contracts.payload.PayloadFile`
-- ``adapter`` + an opaque ``spec``).  The agent payload defines what its
``spec`` means here, so the framework never has to know what an "agent"
is.

The agent payload's full authoring surface lives in one file,
``instances/<inst>/payloads/agent/payload.yml``::

    payload:
      adapter: agent
      spec:
        runtime:        # what the agent executes (llm, prompt, limits, tools)
          tools:        # everything about the agent's tools
            mcp_only: true
            skills: { loading: lazy }
          context:      # what fills the context window, and how it is cached
            prompt_cache: { enabled: true, static_ttl: 1h }
          reasoning:    # thinking models: thinking mode, effort, reasoning in the UI
            thinking: adaptive
          model_settings:  # raw LLM request settings the typed blocks lack
            extra_body: { max_tokens: 16000 }
        ui:           # presentation surface + every UI feature toggle
          chat_history: { enabled: true }
          file_upload:  { enabled: false }
          feedback:     { enabled: true }
          charts:       { enabled: true }
          tool_output:  { expanded: false }
          chat_layout:  { width: 4xl }
        integrations:   # expose the agent to other systems
          a2a:        { enabled: true, card: {...} }
          mcp_server: { enabled: true }

The three section kinds follow a strict taxonomy:

* **Config values** -- what the runtime reads (``model``,
  ``system_prompt``, ``max_*``).
* **Behavioral modes** -- tune a capability that is already present
  (``tools.skills.loading``, ``tools.mcp_only``, ``inject_time_user``,
  ``ui.tool_output.expanded``, ``ui.chat_layout.width``).
* **Mount / exposure toggles** (``enabled``) -- switch infra / an endpoint
  / a UI feature on or off (``ui.*.enabled``, ``integrations.*.enabled``).

Tool *providers* (``skills/``, ``mcp_servers/`` directories, the
``tools.definitions`` list) are enabled by *existing*; they carry no
duplicate ``enabled`` toggle here -- only their behavioral modes do.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from agent_foundry.contracts.a2a import A2AConfig
from agent_foundry.contracts.payload import PayloadBrickRequirement
from agent_foundry.contracts.tools import Tool

# ``extra="forbid"`` on every spec model: a misplaced or misspelled key
# is a contract error and must fail fast (this is a hard-cut 2.0 surface
# with no legacy fallback).  ``A2AIntegration`` is the one exception --
# it inherits ``A2AConfig``'s ``extra="ignore"`` so card authoring stays
# forgiving of forward-compatible card fields.
_SPEC_CONFIG = ConfigDict(extra="forbid")

_TOOLS_MAPPING_MIGRATION = (
    "spec.runtime.tools.definitions must be a list of Tool entries "
    "(type + name + description), not a mapping. "
    "See the definitions example in payloads/agent/_scaffold/payload.yml."
)

# Default prose injected above the lazy-mode skill routing index
# (issue #98).  Instances override it via
# ``spec.runtime.tools.skills.routing_header``; the skills assembler
# (``payloads/agent/deploy/skills/assemble.py``) reads the field and falls
# back to this constant.  Deliberately encourages loading *multiple*
# skills: a single request often spans several skills.
DEFAULT_SKILL_ROUTING_HEADER = (
    "Each row is one skill you can activate by calling "
    "`_load_skill(skill_id=...)`.  The body of a loaded skill tells you "
    "how to use the tools it owns.  Load every skill whose description or "
    "triggers match the request -- many requests need more than one -- "
    "then keep issuing tool calls until it is fully handled."
)


# ---------------------------------------------------------------------------
# Unity Gateway resources (shared by spec.runtime.tools.mcp / .skills)
# ---------------------------------------------------------------------------

# Unity Catalog name parts.  The FQNs end up in gateway URLs and API
# paths, so anything outside this alphabet is rejected at load time.
_UC_NAME_PART = r"[A-Za-z0-9_][A-Za-z0-9_-]*"
SCHEMA_FQN_PATTERN = rf"^{_UC_NAME_PART}\.{_UC_NAME_PART}$"
SECURABLE_FQN_PATTERN = rf"^{_UC_NAME_PART}\.{_UC_NAME_PART}\.{_UC_NAME_PART}$"
# Authored FQNs may also carry ``${var.name}`` placeholders, substituted
# before use (see ``payloads.agent.config.load_agent_spec``); resolved
# names are always re-checked against the strict patterns above.
_VAR_TOKEN = r"\$\{var\.[A-Za-z0-9_]+\}"
_UC_NAME_TEMPLATE = rf"(?:{_VAR_TOKEN}|[A-Za-z0-9_])(?:{_VAR_TOKEN}|[A-Za-z0-9_-])*"
SchemaFqn = Annotated[str, StringConstraints(pattern=rf"^{_UC_NAME_TEMPLATE}\.{_UC_NAME_TEMPLATE}$")]
SecurableFqn = Annotated[
    str, StringConstraints(pattern=rf"^{_UC_NAME_TEMPLATE}\.{_UC_NAME_TEMPLATE}\.{_UC_NAME_TEMPLATE}$")
]

# Who a gateway resource runs as.  Deliberately no ``auto``: an ``obo``
# resource without a user token is dropped, never silently run as the
# app's service principal.
Identity = Literal["obo", "sp"]

McpSource = Literal["workspace", "unity_gateway"]
SkillsSource = Literal["foundry", "unity_gateway"]


class PinnedToolFilter(BaseModel):
    """``pinned[].tools`` -- narrows a pinned MCP service's tools.

    Applied on top of the service's governed tool selection.  An empty
    ``allow`` leaves the service in charge; it does not mean "no tools".
    """

    model_config = _SPEC_CONFIG

    allow: list[str] = Field(default_factory=list)


class PinnedMcp(BaseModel):
    """A gateway MCP service the agent always mounts (locked in the UI)."""

    model_config = _SPEC_CONFIG

    fqn: SecurableFqn
    identity: Identity
    tools: PinnedToolFilter = PinnedToolFilter()


class PinnedSkill(BaseModel):
    """A Unity Catalog skill the agent always loads (locked in the UI)."""

    model_config = _SPEC_CONFIG

    fqn: SecurableFqn
    identity: Identity


class McpLimits(BaseModel):
    """Upper bounds for one MCP discovery block."""

    model_config = _SPEC_CONFIG

    max_servers: int = Field(default=8, ge=1)
    max_tools: int = Field(default=60, ge=1)


class SkillLimits(BaseModel):
    """Upper bounds for one skill discovery block."""

    model_config = _SPEC_CONFIG

    max_skills: int = Field(default=20, ge=1)
    max_loaded_chars: int = Field(default=40_000, ge=1)


class DiscoverySpec(BaseModel):
    """Where gateway resources are looked up, and which ones qualify.

    Shared by ``discovery`` (always the app's service principal) and
    ``user_catalog`` (always the signed-in user).  The identity follows
    from the field the block sits in, so it is not configurable here.
    """

    model_config = _SPEC_CONFIG

    enabled: bool = False
    locations: list[SchemaFqn] = Field(default_factory=list)
    # FQN globs, matched before anything is connected or loaded.
    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _locations_when_enabled(self) -> DiscoverySpec:
        if self.enabled and not self.locations:
            raise ValueError("discovery requires at least one 'catalog.schema' location when enabled")
        return self


class McpDiscoverySpec(DiscoverySpec):
    """``mcp.gateway.discovery`` / ``mcp.gateway.user_catalog``."""

    # Tool-name globs dropped from every service in this block.
    exclude_tools: list[str] = Field(default_factory=list)
    limits: McpLimits = McpLimits()


class SkillDiscoverySpec(DiscoverySpec):
    """``skills.gateway.discovery`` / ``skills.gateway.user_catalog``."""

    limits: SkillLimits = SkillLimits()


class SkillAuthoring(BaseModel):
    """``skills.gateway.authoring`` -- users creating skills from the UI."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    enabled: bool = False
    # ``schema`` would shadow a ``BaseModel`` attribute, hence the alias.
    schema_name: SchemaFqn | None = Field(default=None, alias="schema")
    max_skill_md_chars: int = Field(default=20_000, ge=1)

    @model_validator(mode="after")
    def _schema_when_enabled(self) -> SkillAuthoring:
        if self.enabled and self.schema_name is None:
            raise ValueError("skills.gateway.authoring.schema is required when authoring is enabled")
        return self


def _reject_duplicate_fqns(pinned: Sequence[PinnedMcp | PinnedSkill], field: str) -> None:
    seen: set[str] = set()
    for entry in pinned:
        if entry.fqn in seen:
            raise ValueError(f"{field} lists {entry.fqn!r} more than once")
        seen.add(entry.fqn)


class GatewayMcpRuntime(BaseModel):
    """``spec.runtime.tools.mcp.gateway`` -- MCP services through Unity Gateway."""

    model_config = _SPEC_CONFIG

    pinned: list[PinnedMcp] = Field(default_factory=list)
    discovery: McpDiscoverySpec = McpDiscoverySpec()
    user_catalog: McpDiscoverySpec = McpDiscoverySpec()

    @model_validator(mode="after")
    def _unique_pinned(self) -> GatewayMcpRuntime:
        _reject_duplicate_fqns(self.pinned, "spec.runtime.tools.mcp.gateway.pinned")
        return self


class GatewaySkillsRuntime(BaseModel):
    """``spec.runtime.tools.skills.gateway`` -- Unity Catalog skills."""

    model_config = _SPEC_CONFIG

    pinned: list[PinnedSkill] = Field(default_factory=list)
    discovery: SkillDiscoverySpec = SkillDiscoverySpec()
    user_catalog: SkillDiscoverySpec = SkillDiscoverySpec()
    # How often the service-principal skill block is checked for edits.
    # ``0`` loads it once per process.
    refresh_seconds: int = Field(default=900, ge=0)
    authoring: SkillAuthoring = SkillAuthoring()

    @model_validator(mode="after")
    def _check_pinned_and_authoring(self) -> GatewaySkillsRuntime:
        _reject_duplicate_fqns(self.pinned, "spec.runtime.tools.skills.gateway.pinned")
        schema = self.authoring.schema_name
        if self.authoring.enabled and schema not in self.user_catalog.locations:
            raise ValueError(
                f"skills.gateway.authoring.schema {schema!r} must be one of skills.gateway.user_catalog.locations"
            )
        return self


SkillLoadingMode = Literal["eager", "lazy"]


def effective_skill_loading(authored: SkillLoadingMode | None, *, prompt_cache_enabled: bool) -> SkillLoadingMode:
    """*authored* defaulting to eager; prompt caching overrides lazy, for every skill source."""
    mode = authored or "eager"
    return "eager" if prompt_cache_enabled and mode == "lazy" else mode


def _require_user_catalog_only(gateway: GatewaySkillsRuntime) -> None:
    if gateway.pinned or gateway.discovery.enabled or gateway.authoring.enabled:
        raise ValueError(
            "spec.runtime.tools.skills.gateway with source: foundry allows only user_catalog; "
            "pinned, discovery and authoring require source: unity_gateway "
            "(or use skills.unity_catalog next to skills.repository)"
        )


class RepositorySkills(BaseModel):
    """``spec.runtime.tools.skills.repository`` -- the payload's ``skills/`` folder."""

    model_config = _SPEC_CONFIG

    enabled: bool = True


class UnityCatalogSkills(BaseModel):
    """``spec.runtime.tools.skills.unity_catalog`` -- the developer's Unity Catalog skills."""

    model_config = _SPEC_CONFIG

    enabled: bool = True
    pinned: list[PinnedSkill] = Field(default_factory=list)
    discovery: SkillDiscoverySpec = SkillDiscoverySpec()
    # How often the service-principal skills are re-checked for edits.
    # Unchanged skills render the same bytes, so the prompt cache only
    # busts on a real change.  ``0`` reads them once per process.
    refresh_seconds: int = Field(default=900, ge=0)

    @model_validator(mode="after")
    def _unique_pinned(self) -> UnityCatalogSkills:
        _reject_duplicate_fqns(self.pinned, "spec.runtime.tools.skills.unity_catalog.pinned")
        return self


# ---------------------------------------------------------------------------
# spec.runtime
# ---------------------------------------------------------------------------


UserSkillStore = Literal["lakebase", "unity_catalog"]


class UserSkills(BaseModel):
    """``spec.runtime.tools.skills.user_skills`` -- skills each user adds for their own chats.

    ``store: lakebase`` keeps them per user in the chat-history Lakebase.
    ``store: unity_catalog`` lets users pick from ``locations`` and, with
    ``authoring_schema``, write their own there; inside Databricks Apps
    the user token lacks the ``unity-catalog`` scope that needs.

    ``enabled`` is the switch.  The deprecated form sets only ``store``.
    """

    model_config = _SPEC_CONFIG

    enabled: bool | None = None
    store: UserSkillStore = "lakebase"
    max_skills: int = Field(default=20, ge=1)
    max_skill_md_chars: int = Field(default=20_000, ge=1)
    locations: list[SchemaFqn] = Field(default_factory=list)
    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)
    authoring_schema: SchemaFqn | None = None

    @model_validator(mode="after")
    def _check_store_fields(self) -> UserSkills:
        catalog_fields = bool(self.locations or self.include or self.exclude or self.authoring_schema)
        if self.store == "lakebase" and catalog_fields:
            raise ValueError(
                "user_skills.locations, include, exclude and authoring_schema require store: unity_catalog"
            )
        if self.store == "unity_catalog" and self.is_enabled() and not self.locations:
            raise ValueError("user_skills.store: unity_catalog requires at least one 'catalog.schema' location")
        if self.authoring_schema is not None and self.authoring_schema not in self.locations:
            raise ValueError(f"user_skills.authoring_schema {self.authoring_schema!r} must be one of its locations")
        return self

    def is_enabled(self) -> bool:
        return self.enabled if self.enabled is not None else "store" in self.model_fields_set

    def store_is_deprecated(self) -> bool:
        return self.enabled is None and "store" in self.model_fields_set

    def catalog_spec(self) -> SkillDiscoverySpec:
        return SkillDiscoverySpec(
            enabled=True,
            locations=self.locations,
            include=self.include,
            exclude=self.exclude,
            limits=SkillLimits(max_skills=self.max_skills),
        )

    def authoring_spec(self) -> SkillAuthoring:
        return SkillAuthoring(
            enabled=self.authoring_schema is not None,
            schema_name=self.authoring_schema,
            max_skill_md_chars=self.max_skill_md_chars,
        )


class AgentSkillsRuntime(BaseModel):
    """``spec.runtime.tools.skills`` -- where the agent's skills come from.

    The developer's skills come from ``repository`` (the ``skills/``
    folder, on by default) and ``unity_catalog``; both may be on.  The
    user's own come from ``user_skills``, one store at a time.

    ``source`` and ``gateway`` are deprecated: ``source: unity_gateway``
    means no repository skills, and ``gateway`` holds the Unity Catalog
    skills (with ``source: foundry`` only ``user_catalog``).  Readers use
    :meth:`repository_enabled`, :meth:`effective_gateway` and
    :meth:`lakebase_user_skills`, which answer for either form.
    """

    model_config = _SPEC_CONFIG

    # Skill-loading MODE (issue #98).  ``None`` -> the assembler defaults
    # to ``"eager"``.  ``lazy`` swaps the system prompt for a routing
    # index and defers skill bodies to the ``_load_skill`` tool.
    loading: SkillLoadingMode | None = None
    # Prose shown above the lazy-mode routing index.  ``None`` -> the
    # assembler falls back to ``DEFAULT_SKILL_ROUTING_HEADER``.  Only
    # consumed in lazy mode.
    routing_header: str | None = None
    repository: RepositorySkills | None = None
    unity_catalog: UnityCatalogSkills | None = None
    user_skills: UserSkills = UserSkills()
    # Deprecated: replaced by ``repository`` / ``unity_catalog`` / ``user_skills``.
    source: SkillsSource | None = None
    gateway: GatewaySkillsRuntime | None = None

    @model_validator(mode="after")
    def _check_skill_sources(self) -> AgentSkillsRuntime:
        legacy = self.source is not None or self.gateway is not None
        if legacy and (self.repository is not None or self.unity_catalog is not None):
            raise ValueError(
                "spec.runtime.tools.skills: use repository / unity_catalog or the deprecated source / gateway, not both"
            )
        if self.gateway is not None and self.source != "unity_gateway":
            _require_user_catalog_only(self.gateway)
        if self.gateway is not None and self.gateway.user_catalog.enabled and self.user_skills.is_enabled():
            raise ValueError(
                "spec.runtime.tools.skills: pick one user skill store, "
                "user_skills or the deprecated gateway.user_catalog, not both"
            )
        return self

    def repository_enabled(self) -> bool:
        """Whether the ``skills/`` folder is read."""
        if self.repository is not None:
            return self.repository.enabled
        return self.source != "unity_gateway"

    def effective_gateway(self) -> GatewaySkillsRuntime | None:
        """The Unity Catalog skills the agent reads, or ``None`` without any."""
        if self.gateway is not None:
            return self.gateway
        if self.source == "unity_gateway":
            return GatewaySkillsRuntime()
        return self._gateway_from_branches()

    def lakebase_user_skills(self) -> UserSkills | None:
        """``user_skills`` when users keep their own skills in Lakebase."""
        on = self.user_skills.is_enabled() and self.user_skills.store == "lakebase"
        return self.user_skills if on else None

    def deprecated_fields(self) -> list[str]:
        names = [name for name in ("source", "gateway") if getattr(self, name) is not None]
        if self.user_skills.store_is_deprecated():
            names.append("user_skills.store without user_skills.enabled")
        return names

    def _gateway_from_branches(self) -> GatewaySkillsRuntime | None:
        developer = self.unity_catalog if self.unity_catalog is not None and self.unity_catalog.enabled else None
        user = self.user_skills if self.user_skills.is_enabled() and self.user_skills.store == "unity_catalog" else None
        if developer is None and user is None:
            return None
        fields: dict[str, Any] = {}
        if developer is not None:
            fields.update(
                pinned=developer.pinned, discovery=developer.discovery, refresh_seconds=developer.refresh_seconds
            )
        if user is not None:
            fields.update(user_catalog=user.catalog_spec(), authoring=user.authoring_spec())
        return GatewaySkillsRuntime(**fields)


class AgentMcpRuntime(BaseModel):
    """``spec.runtime.tools.mcp`` -- the agent's Unity Gateway MCP services.

    Presence-based, like every tool provider: ``mcp_servers/*/server.yml``
    files mount workspace servers and a ``gateway`` block mounts gateway
    services.  Both may be present; workspace tool names are claimed first.

    ``source`` is deprecated.  ``unity_gateway`` without a ``gateway``
    block still means an empty gateway block; ``workspace`` means nothing.
    """

    model_config = _SPEC_CONFIG

    source: McpSource = "workspace"
    gateway: GatewayMcpRuntime | None = None

    def effective_gateway(self) -> GatewayMcpRuntime | None:
        """The gateway block the agent mounts, or ``None`` without one."""
        if self.gateway is not None:
            return self.gateway
        return GatewayMcpRuntime() if self.source == "unity_gateway" else None

    def source_is_deprecated(self) -> bool:
        return "source" in self.model_fields_set


class AgentToolsRuntime(BaseModel):
    """``spec.runtime.tools`` -- everything about the agent's tools.

    Tool providers live in their own authoring surfaces (``skills/`` and
    ``mcp_servers/`` directories, the ``definitions`` list); this block
    carries the cross-cutting *sourcing strategy* and per-provider modes.
    """

    model_config = _SPEC_CONFIG

    # Tool-sourcing strategy: when true (default), the runtime suppresses
    # the direct tool factories and attaches tools exclusively via MCP
    # servers.  Opt out (``false``) to keep direct Genie / Vector Search
    # / UC Function tools.
    mcp_only: bool | None = None
    # Direct tool definitions (the provider that used to be the
    # ``tools:`` list in config.yml).  Presence-based: omit when the
    # agent's tools come only from skills + MCP servers.
    definitions: list[Tool] | None = None
    skills: AgentSkillsRuntime = AgentSkillsRuntime()
    mcp: AgentMcpRuntime = AgentMcpRuntime()

    @field_validator("definitions", mode="before")
    @classmethod
    def _reject_mapping_tools(cls, value: Any) -> Any:
        """Reject the legacy ``dict[str, Any]`` tools form with a hint."""
        if value is None or isinstance(value, list):
            return value
        if isinstance(value, Mapping):
            raise ValueError(_TOOLS_MAPPING_MIGRATION)
        return value


class AgentPromptCacheRuntime(BaseModel):
    """``spec.runtime.context.prompt_cache`` -- provider prompt caching.

    Marks the stable prefix of each request (tools + system prompt) and
    the conversation tail with ``cache_control`` breakpoints so the
    provider bills them as cache reads instead of fresh input.

    The TTLs are a closed set rather than free strings on purpose: a
    typo would be accepted by the gateway and silently turn every
    request into a full-price cache write.
    """

    model_config = _SPEC_CONFIG

    # Break-glass switch.  ``None`` -> the ``AgentConfig`` default (on).
    enabled: bool | None = None
    # TTL for the deployment-stable prefix (tools + system prompt).
    static_ttl: Literal["5m", "1h"] | None = None
    # TTL for the conversation-history breakpoints.
    rolling_ttl: Literal["5m", "1h"] | None = None


class AgentDataUniverseRuntime(BaseModel):
    """``spec.runtime.context.data_universe`` -- inject a catalog overview.

    Reads a Unity Catalog table function -- one row per queryable view,
    with its location, size and description -- and renders it into the
    system prompt at request assembly.  The agent then knows what data
    exists without spending a tool call to ask.

    The *SQL* deliberately lives in the UC function, not here: the
    runtime only ever issues ``SELECT * FROM <function>()`` and renders
    whatever columns come back, so the projection, wording, filtering and
    row order are all changed with a ``CREATE OR REPLACE FUNCTION``
    through the ``sql_function`` brick -- no app redeploy.

    The block joins the prompt-cached prefix, which is what makes it
    nearly free to carry, and also what makes it a liability if it is not
    byte-stable: see ``refresh_minutes``.

    The SQL read is opt-in.  ``enabled`` must be set explicitly whenever
    any other field in this block is authored, so a catalog query cannot
    be configured by accident.
    """

    model_config = _SPEC_CONFIG

    # Opt-in.  ``None`` (block omitted) and ``False`` both skip the SQL
    # read.  Required whenever any other field below is set.
    enabled: bool | None = None
    # Table function to read.  A bare name resolves against the
    # instance's ``agent_schema``; a dotted name is used as authored.
    function: str | None = None
    # Markdown heading the block is rendered under.
    heading: str | None = None
    # Hard row cap.  Truncation is announced in the block rather than
    # silent, so a growing catalog is visible in the prompt.
    max_rows: int | None = Field(default=None, ge=1)
    # Budget for the read.  On expiry the previous block is served.
    timeout_seconds: int | None = Field(default=None, ge=1)
    # How often to re-read the function.  A re-read only costs a cache
    # write when the content actually changed, so this is cheap; ``0``
    # pins the block for the process lifetime.
    refresh_minutes: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _enabled_required_when_configured(self) -> AgentDataUniverseRuntime:
        configured = any(
            value is not None
            for value in (
                self.function,
                self.heading,
                self.max_rows,
                self.timeout_seconds,
                self.refresh_minutes,
            )
        )
        if configured and self.enabled is None:
            raise ValueError(
                "runtime.context.data_universe.enabled must be set explicitly "
                "when any other data_universe field is authored; the SQL read "
                "is opt-in"
            )
        return self


class AgentToolResultClearingRuntime(BaseModel):
    """``spec.runtime.context.tool_result_clearing`` -- client-side trimming.

    Replaces older ``function_call_output`` payloads with a short
    placeholder immediately before each model call.  The full transcript
    in chat history is untouched; only the in-flight request is rewritten.

    Clearing arms on either of two independent triggers -- see the field
    comments -- because neither alone covers every conversation shape.
    """

    model_config = _SPEC_CONFIG

    # Break-glass switch.  ``None`` -> the ``AgentConfig`` default (on).
    enabled: bool | None = None
    # How many recent rounds of tool calls keep their results verbatim; a
    # round is everything one model call asked for, in parallel or not.
    keep_tool_uses: int | None = Field(default=None, ge=0)
    # Savings-available trigger: minimum net characters recovered by one
    # clearing pass.  Prevents small wins from repeatedly invalidating the
    # rolling prompt cache.  ``0`` clears whenever anything is clearable.
    clear_at_least_chars: int | None = Field(default=None, ge=0)
    # Occupancy trigger: clear every eligible result once the outbound
    # request is estimated to carry at least this many prompt tokens.  The
    # in-turn emergency valve -- compaction only runs between turns, so
    # without it one tool-heavy turn reaches the provider's window limit
    # and fails unrecoverably.  ``0`` disables it.  Lower this for
    # endpoints whose window is smaller than 200k.
    clear_above_input_tokens: int | None = Field(default=None, ge=0)


# Defaults the effective compaction trigger is derived from; see
# ``AgentContextRuntime.compaction_trigger_input_tokens``.
DEFAULT_CLEAR_ABOVE_INPUT_TOKENS: Final = 150_000
DEFAULT_COMPACTION_TRIGGER_INPUT_TOKENS: Final = 120_000
COMPACTION_TRIGGER_HEADROOM_TOKENS: Final = 20_000
MAX_COMPACTION_INSTRUCTIONS_CHARS: Final = 2_000


class AgentCompactionRuntime(BaseModel):
    """``spec.runtime.context.compaction`` -- summarize older turns of long chats.

    The chat UI server replaces everything before the most recent turns
    with a persisted summary once a request grows past the trigger.  The
    summary lives in Lakebase next to the chat, so compaction needs
    ``spec.ui.chat_history``; the transcript itself is never modified.
    """

    model_config = _SPEC_CONFIG

    # ``None`` -> on exactly when ``spec.ui.chat_history`` is on.
    enabled: bool | None = None
    # Estimated prompt tokens at which a chat is compacted after the turn.
    # Must stay below ``tool_result_clearing.clear_above_input_tokens``:
    # compaction is the normal valve, clearing the in-turn emergency one.
    # ``None`` -> derived, see
    # ``AgentContextRuntime.compaction_trigger_input_tokens``.
    trigger_input_tokens: int | None = Field(default=None, ge=1)
    # How many of the most recent user turns stay verbatim.
    keep_recent_turns: int = Field(default=3, ge=1)
    # Instance-specific rules appended to the fixed summary prompt.
    instructions: str | None = Field(default=None, max_length=MAX_COMPACTION_INSTRUCTIONS_CHARS)
    # Show the "Compact conversation" action in the chat header.  Off by
    # default: compaction runs on its own, and the chat shows it happening.
    manual_action: bool = False


MAX_MEMORY_INDEX_ENTRIES: Final = 100
_MEMORY_STORE_REMOVED: Final = "was removed: memory is kept in the chat-history Lakebase (#787)"

# Keys that left ``spec.runtime.context.memory``, with where they went.
_MOVED_MEMORY_KEYS: Final[dict[str, str]] = {
    "store": _MEMORY_STORE_REMOVED,
    "plan": "moved to spec.runtime.context.plan.enabled",
    "user": "was removed: memory is always the user's, across chats",
    "chat": "was removed: per-chat working state is the plan (spec.runtime.context.plan)",
}


class AgentPlanRuntime(BaseModel):
    """``spec.runtime.context.plan`` -- the agent's goal and milestones for long work in one chat.

    Kept with the chat in the chat-history Lakebase and deleted with it, so
    it needs ``spec.ui.chat_history``.
    """

    model_config = _SPEC_CONFIG

    enabled: bool = False
    # Longest stored plan; also caps the finished plans kept for the chat.
    max_chars: int = Field(default=8_000, ge=1)


class AgentMemoryRuntime(BaseModel):
    """``spec.runtime.context.memory`` -- long-term facts about the signed-in user, across chats.

    Kept per user in the chat-history Lakebase, so it needs
    ``spec.ui.chat_history``.  The state of the current work is the plan's
    (``spec.runtime.context.plan``).
    """

    model_config = _SPEC_CONFIG

    enabled: bool = False
    # Longest topic: its facts, one per line.
    max_entry_chars: int = Field(default=8_000, ge=1)
    # Topics listed with their fact counts at the start of each request.
    index_max_entries: int = Field(default=50, ge=1, le=MAX_MEMORY_INDEX_ENTRIES)

    @model_validator(mode="before")
    @classmethod
    def _reject_moved_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            moved = [f"memory.{key} {where}" for key, where in _MOVED_MEMORY_KEYS.items() if key in data]
            if moved:
                raise ValueError("; ".join(moved))
        return data


class AgentContextRuntime(BaseModel):
    """``spec.runtime.context`` -- what fills the context window, and how.

    Groups the levers that decide which bytes reach the model on every
    request and how they are cached.  Whether the user/time identity is
    injected at all remains ``spec.runtime.inject_time_user``; *where*
    it lands is not authorable, because it must sit behind the cached
    prefix for caching to work at all.
    """

    model_config = _SPEC_CONFIG

    prompt_cache: AgentPromptCacheRuntime = AgentPromptCacheRuntime()
    data_universe: AgentDataUniverseRuntime = AgentDataUniverseRuntime()
    tool_result_clearing: AgentToolResultClearingRuntime = AgentToolResultClearingRuntime()
    compaction: AgentCompactionRuntime = AgentCompactionRuntime()
    plan: AgentPlanRuntime = AgentPlanRuntime()
    memory: AgentMemoryRuntime = AgentMemoryRuntime()

    @model_validator(mode="before")
    @classmethod
    def _reject_memory_store(cls, data: Any) -> Any:
        if isinstance(data, dict) and "memory_store" in data:
            raise ValueError(f"memory_store {_MEMORY_STORE_REMOVED}")
        return data

    def clear_above_input_tokens(self) -> int:
        """Effective clearing occupancy threshold; ``0`` when it never fires."""
        clearing = self.tool_result_clearing
        if clearing.enabled is False:
            return 0
        if clearing.clear_above_input_tokens is None:
            return DEFAULT_CLEAR_ABOVE_INPUT_TOKENS
        return clearing.clear_above_input_tokens

    def compaction_trigger_input_tokens(self) -> int:
        """Effective compaction trigger: authored, or derived below clearing."""
        authored = self.compaction.trigger_input_tokens
        if authored is not None:
            return authored
        clear_above = self.clear_above_input_tokens()
        if clear_above == 0:
            return DEFAULT_COMPACTION_TRIGGER_INPUT_TOKENS
        derived = max(1, clear_above - COMPACTION_TRIGGER_HEADROOM_TOKENS)
        return min(DEFAULT_COMPACTION_TRIGGER_INPUT_TOKENS, derived)

    @model_validator(mode="after")
    def _compaction_triggers_before_clearing(self) -> AgentContextRuntime:
        authored = self.compaction.trigger_input_tokens
        clear_above = self.clear_above_input_tokens()
        if authored is None or clear_above == 0 or authored < clear_above:
            return self
        raise ValueError(
            f"runtime.context.compaction.trigger_input_tokens ({authored}) must stay below "
            f"tool_result_clearing.clear_above_input_tokens ({clear_above}); "
            "compaction is the normal valve, clearing the in-turn emergency one"
        )


class AgentReasoningRuntime(BaseModel):
    """``spec.runtime.reasoning`` -- thinking models and their reasoning.

    Every field is optional and nothing is sent when it is unset, so the
    endpoint's own default applies and non-thinking models see a request
    byte-identical to one without this block.  Combinations the endpoint is
    known to reject fail here, at load time; anything else is the
    endpoint's call and comes back as a 400 from the model.
    """

    model_config = _SPEC_CONFIG

    # Sent as ``thinking: {type: ...}``.  ``adaptive`` lets the model
    # decide per turn; ``between_tools`` limits thinking to the short
    # updates between tool calls; ``enabled`` thinks on every turn within
    # ``budget_tokens``.  Claude 5.x on Databricks (Sonnet/Haiku/Opus 5)
    # rejects ``enabled`` and ``disabled``: use ``adaptive`` and ``effort``.
    thinking: Literal["adaptive", "between_tools", "enabled", "disabled"] | None = None
    # Token budget for ``thinking: enabled`` -- required there, not allowed
    # elsewhere.  The request's ``max_tokens`` (``model_settings.extra_body``)
    # must be larger than this.
    budget_tokens: int | None = Field(default=None, ge=1024)
    # Sent as ``thinking: {display: ...}``: whether the reasoning comes
    # back as readable summary text (``summarized``) or empty
    # (``omitted``).  Newer models (Claude Sonnet 5.5) return it empty
    # unless asked, so ``None`` means ``summarized`` while ``emit`` is on
    # and ``thinking`` is ``adaptive`` or ``enabled``.
    display: Literal["summarized", "omitted"] | None = None
    # Sent as ``reasoning_effort``.  On Claude 5.x this is the supported
    # way to scale thinking (the endpoint's error text also names
    # ``output_config.effort``; chat-completions takes ``reasoning_effort``).
    effort: Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"] | None = None
    # Whether ``/invocations`` streams the model's reasoning, which the
    # chat UI renders as a collapsible section.  ``None`` -> on.  A caller
    # can override it per request with ``custom_inputs.include_reasoning``.
    # Also shapes the request: it decides the default ``display``.
    emit: bool | None = None

    @model_validator(mode="after")
    def _reject_known_bad_combinations(self) -> AgentReasoningRuntime:
        if self.budget_tokens is not None and self.thinking != "enabled":
            raise ValueError("spec.runtime.reasoning.budget_tokens requires thinking: enabled")
        if self.thinking == "enabled" and self.budget_tokens is None:
            raise ValueError("spec.runtime.reasoning.thinking: enabled requires budget_tokens")
        if self.display is not None:
            # The endpoint rejects a thinking block without ``type``.
            if self.thinking is None:
                raise ValueError("spec.runtime.reasoning.display requires thinking to be set")
            # ``between_tools`` takes no ``display`` (endpoint 400), and
            # ``disabled`` produces nothing to display.
            if self.thinking in {"between_tools", "disabled"}:
                raise ValueError(f"spec.runtime.reasoning.display is not allowed with thinking: {self.thinking}")
        return self


# Databricks FMAPI / Unity Gateway Claude 5.x (e.g. claude-sonnet-5-5).
# Matching is on the model id substring so serving-endpoint names and
# ``system.ai.*`` FQNs share one check.
_CLAUDE_5_MODEL_MARKERS: tuple[str, ...] = (
    "claude-sonnet-5",
    "claude-haiku-5",
    "claude-opus-5",
)


def _is_claude_5_model(name: str) -> bool:
    """Whether *name* is a Claude 5.x id that rejects thinking.enabled."""
    lowered = name.lower()
    return any(marker in lowered for marker in _CLAUDE_5_MODEL_MARKERS)


# Request keys the Agents SDK owns, or that ``spec.runtime.reasoning``
# already sets.  Passing them through ``extra_body`` would either fight
# the SDK or silently override the typed field.
_RESERVED_EXTRA_BODY_KEYS: dict[str, str] = {
    "model": "spec.runtime.model",
    "messages": "the Agents SDK",
    "tools": "the Agents SDK",
    "tool_choice": "the Agents SDK",
    "stream": "the Agents SDK",
    "stream_options": "the Agents SDK",
    "thinking": "spec.runtime.reasoning.thinking",
    "reasoning_effort": "spec.runtime.reasoning.effort",
}


class AgentModelSettingsRuntime(BaseModel):
    """``spec.runtime.model_settings`` -- raw request settings for the LLM.

    The escape hatch for endpoint parameters the typed blocks do not
    cover (e.g. ``max_tokens``, ``temperature``).  ``extra_body`` is merged
    into every chat-completions request body unvalidated, so a typo is
    only caught by the endpoint.
    """

    model_config = _SPEC_CONFIG

    extra_body: dict[str, Any] | None = None

    @field_validator("extra_body")
    @classmethod
    def _reject_reserved_keys(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        for key in value or {}:
            owner = _RESERVED_EXTRA_BODY_KEYS.get(key)
            if owner is not None:
                raise ValueError(f"spec.runtime.model_settings.extra_body.{key} is set by {owner}; set it there")
        return value


class ServingEndpointModel(BaseModel):
    """``spec.runtime.model`` with ``kind: serving_endpoint``."""

    model_config = _SPEC_CONFIG

    kind: Literal["serving_endpoint"]
    name: str = Field(min_length=1)


class ModelServiceModel(BaseModel):
    """``spec.runtime.model`` with ``kind: model_service`` (Unity Gateway)."""

    model_config = _SPEC_CONFIG

    kind: Literal["model_service"]
    name: Annotated[str, StringConstraints(pattern=SECURABLE_FQN_PATTERN)]


AgentModel = Annotated[ServingEndpointModel | ModelServiceModel, Field(discriminator="kind")]


class AgentRuntimeSpec(BaseModel):
    """``spec.runtime`` -- what the agent executes.

    Every scalar is optional: the runtime view
    :class:`payloads.agent.config.AgentConfig` applies framework-level
    defaults for any field left unset.
    """

    model_config = _SPEC_CONFIG

    model: AgentModel | None = None
    system_prompt: str | None = None
    max_tool_calls_per_turn: int | None = None
    max_iterations: int | None = None
    inject_time_user: bool | None = None
    tools: AgentToolsRuntime = AgentToolsRuntime()
    context: AgentContextRuntime = AgentContextRuntime()
    reasoning: AgentReasoningRuntime = AgentReasoningRuntime()
    model_settings: AgentModelSettingsRuntime = AgentModelSettingsRuntime()

    # Optional lifecycle hooks (dotted import paths, same mechanism and
    # the same fail-closed instance-dir check as ``type: python`` tools:
    # the callable must be instance code under ``instances/<name>/``).
    # Both are generic -- the framework never
    # inspects what they do -- and let an instance return structured
    # output out-of-band without stuffing it into prose.
    #
    # Each hook may declare a ``state`` parameter to receive the request's
    # ``RequestState`` -- the same object passed to the run as ``context``
    # and to every ``type: python`` tool -- plus ``request`` (and
    # ``finalize`` also ``result``); the runtime passes only the parameters
    # the callable declares.  ``reset`` runs at request start (seed
    # ``state.data`` before any tool runs); ``finalize`` runs after
    # ``Runner.run`` completes (read ``state.data`` back) and its returned
    # dict is merged into ``ResponsesAgentResponse.custom_outputs``.
    # ``state`` is the only supported per-request accumulator.  Only
    # consumed on the ``@invoke`` / ``@stream`` path.
    finalize: str | None = None
    reset: str | None = None

    @model_validator(mode="after")
    def _claude_5_thinking_modes(self) -> AgentRuntimeSpec:
        # Live 400 from databricks-claude-sonnet-5-5 / system.ai.claude-sonnet-5-5:
        # ``thinking.type.enabled`` is not supported; use adaptive + effort.
        # The same family rejects ``disabled`` (the model always thinks).
        thinking = self.reasoning.thinking
        if thinking not in {"enabled", "disabled"}:
            return self
        if self.model is None or not _is_claude_5_model(self.model.name):
            return self
        name = self.model.name
        raise ValueError(
            f"spec.runtime.reasoning.thinking: {thinking} is not supported on Claude 5.x "
            f"({name}). Use thinking: adaptive and reasoning.effort; "
            "budget_tokens is not available on this model "
            "(set model_settings.extra_body.max_tokens for the completion budget)."
        )

    @model_validator(mode="after")
    def _budget_fits_max_tokens(self) -> AgentRuntimeSpec:
        # The two halves live in different blocks, so only the parent can
        # see both; the endpoint rejects a budget that leaves no room.
        budget = self.reasoning.budget_tokens
        max_tokens = (self.model_settings.extra_body or {}).get("max_tokens")
        if budget is None or max_tokens is None:
            return self
        # ``bool`` is an ``int`` subclass, but never a token count.
        if not isinstance(max_tokens, int) or isinstance(max_tokens, bool):
            raise ValueError(
                "spec.runtime.model_settings.extra_body.max_tokens must be an integer "
                "when spec.runtime.reasoning.budget_tokens is set"
            )
        if budget >= max_tokens:
            raise ValueError(
                "spec.runtime.reasoning.budget_tokens must be smaller than "
                "spec.runtime.model_settings.extra_body.max_tokens"
            )
        return self


# ---------------------------------------------------------------------------
# spec.ui
# ---------------------------------------------------------------------------


class ChatHistoryUI(BaseModel):
    """``spec.ui.chat_history`` -- persistent chat history via Lakebase."""

    model_config = _SPEC_CONFIG

    enabled: bool = True
    # Optional Lakebase project-name override; empty -> the payload
    # derives ``<target>-<app>-lakebase``.
    project_name: str | None = None
    # The sidebar lists only chats created in the last N days; older ones
    # stay reachable by URL.  ``None`` lists every chat.
    sidebar_days: int | None = Field(default=30, ge=1)
    # Lakebase deletes chats (with messages and votes) whose last message
    # is older than N days.  ``None`` keeps every chat.
    retention_days: int | None = Field(default=365, ge=1)

    @model_validator(mode="after")
    def _check_retention_covers_sidebar(self) -> ChatHistoryUI:
        if self.sidebar_days is None or self.retention_days is None:
            return self
        if self.retention_days < self.sidebar_days:
            raise ValueError(
                f"chat_history.retention_days ({self.retention_days}) must be >= "
                f"chat_history.sidebar_days ({self.sidebar_days})"
            )
        return self


class FileUploadUI(BaseModel):
    """``spec.ui.file_upload`` -- chat file-upload + extractor service."""

    model_config = _SPEC_CONFIG

    enabled: bool = False
    # Hard upload size cap (megabytes).
    max_mb: int = 10
    # Also accept PNG / JPEG / WebP images, sent to the model as image
    # content instead of extracted text.  Needs ``enabled`` and a vision
    # model; text-only models reject image input.
    images: bool = False


class FeatureToggle(BaseModel):
    """A plain on/off UI feature (``spec.ui.feedback`` / ``spec.ui.charts``)."""

    model_config = _SPEC_CONFIG

    enabled: bool = True


class ReportsUI(BaseModel):
    """``spec.ui.reports`` -- Markdown reports in the chat.

    Off by default.  Renders ``markdown-report`` fences as a report card the
    user can copy or download as ``.md``, and offers the agent ``_report``.
    """

    model_config = _SPEC_CONFIG

    enabled: bool = False


class ToolOutputUI(BaseModel):
    """``spec.ui.tool_output`` -- how tool-call panels render in the chat.

    A *behavioral mode* rather than a mount toggle: the panels are always
    present, and this only decides whether each one starts expanded.

    Collapsed by default because a turn with several tool calls otherwise
    pushes the actual answer off-screen behind raw parameter / result
    dumps.  Either way the panel header stays clickable, so a user can
    open any individual call to inspect it.
    """

    model_config = _SPEC_CONFIG

    expanded: bool = False


ChatWidth = Literal["4xl", "5xl", "6xl", "7xl"]
"""Allowed chat column widths: the Tailwind ``max-w-*`` scale steps."""


class ChatLayoutUI(BaseModel):
    """``spec.ui.chat_layout`` -- how wide the chat column may grow.

    A *behavioral mode*: the column always fills the window up to this
    cap, then stays centred.  The cap keeps assistant answers readable on
    wide monitors; ``4xl`` is 896px, ``5xl`` 1024px, ``6xl`` 1152px and
    ``7xl`` 1280px.  The empty state, the message list and the composer
    share the one value so they stay aligned.
    """

    model_config = _SPEC_CONFIG

    width: ChatWidth = "4xl"


class ResourcesSidebarUI(BaseModel):
    """``spec.ui.resources_sidebar`` -- the chat's resources panel.

    Off by default.  Lists the agent's MCPs and skills; users can add from
    their own catalog only where a gateway ``user_catalog`` is enabled.
    """

    model_config = _SPEC_CONFIG

    enabled: bool = False


SpeechBackend = Literal["serving_invocations"]
"""Transcription backends: a Model Serving endpoint taking base64 audio (Whisper)."""


class SpeechInputUI(BaseModel):
    """``spec.ui.speech_input`` -- the composer mic button (speech to text).

    Off by default.  The recording is transcribed by ``endpoint`` and the
    text lands in the composer for review; nothing is sent automatically.
    ``language`` is an optional hint such as ``de``; unset, the browser
    language is the hint and the model still auto-detects.
    """

    model_config = _SPEC_CONFIG

    enabled: bool = False
    backend: SpeechBackend = "serving_invocations"
    endpoint: str = ""
    max_seconds: int = Field(default=300, ge=1, le=300)
    language: str | None = Field(default=None, pattern=r"^[A-Za-z]{2}$")

    @model_validator(mode="after")
    def _enabled_needs_endpoint(self) -> SpeechInputUI:
        if self.enabled and not self.endpoint.strip():
            raise ValueError("ui.speech_input.enabled requires ui.speech_input.endpoint")
        return self


class AgentUISpec(BaseModel):
    """``spec.ui`` -- the presentation surface and its feature toggles.

    ``enabled: false`` makes the payload headless (the API is served but
    the bundled chat UI is not).  Each nested feature is a mount toggle.
    """

    model_config = _SPEC_CONFIG

    enabled: bool = True
    kind: str = "chat"
    chat_history: ChatHistoryUI = ChatHistoryUI()
    file_upload: FileUploadUI = FileUploadUI()
    feedback: FeatureToggle = FeatureToggle()
    charts: FeatureToggle = FeatureToggle()
    reports: ReportsUI = ReportsUI()
    tool_output: ToolOutputUI = ToolOutputUI()
    chat_layout: ChatLayoutUI = ChatLayoutUI()
    resources_sidebar: ResourcesSidebarUI = ResourcesSidebarUI()
    speech_input: SpeechInputUI = SpeechInputUI()


# ---------------------------------------------------------------------------
# spec.integrations
# ---------------------------------------------------------------------------


class A2AIntegration(A2AConfig):
    """``spec.integrations.a2a`` -- expose the agent via an A2A card.

    Extends :class:`~agent_foundry.contracts.a2a.A2AConfig` (card +
    ``enable_v0_3_compat``) with the ``enabled`` mount toggle that
    decides whether the ``/api/a2a`` routes + agent card are served.
    """

    enabled: bool = False


class MCPServerIntegration(BaseModel):
    """``spec.integrations.mcp_server`` -- expose the agent AS an MCP server.

    The exposure half of MCP (mounting ``/mcp`` for other systems to
    consume), mirroring ``a2a``.  The *consumption* half (the agent
    using external MCP servers as tools) is presence-based via the
    ``mcp_servers/`` directory and has no toggle here.
    """

    model_config = _SPEC_CONFIG

    enabled: bool = False


class AgentIntegrationsSpec(BaseModel):
    """``spec.integrations`` -- expose the agent to other systems."""

    model_config = _SPEC_CONFIG

    a2a: A2AIntegration = A2AIntegration()
    mcp_server: MCPServerIntegration = MCPServerIntegration()


# ---------------------------------------------------------------------------
# spec + envelope
# ---------------------------------------------------------------------------


class AgentPayloadSpec(BaseModel):
    """``payload.spec`` for the ``agent`` adapter (the full authoring body)."""

    model_config = _SPEC_CONFIG

    runtime: AgentRuntimeSpec = AgentRuntimeSpec()
    ui: AgentUISpec = AgentUISpec()
    integrations: AgentIntegrationsSpec = AgentIntegrationsSpec()
    # Opaque per-extension config, keyed by extension name.  The agent
    # payload never reads it; a wrapping payload adapter and its runtime
    # extension (``AGENT_EXTENSIONS``) validate their own block.
    extensions: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _user_skills_have_lakebase_and_panel(self) -> AgentPayloadSpec:
        if self.runtime.tools.skills.lakebase_user_skills() is None:
            return self
        if not self.ui.chat_history.enabled:
            raise ValueError("user_skills.store: lakebase requires spec.ui.chat_history.enabled")
        if not self.ui.resources_sidebar.enabled:
            raise ValueError("user_skills.store: lakebase requires spec.ui.resources_sidebar.enabled")
        return self

    @model_validator(mode="after")
    def _compaction_has_chat_history(self) -> AgentPayloadSpec:
        if self.runtime.context.compaction.enabled and not self.ui.chat_history.enabled:
            raise ValueError("runtime.context.compaction.enabled requires spec.ui.chat_history.enabled")
        return self

    def compaction_enabled(self) -> bool:
        """Effective compaction toggle: follows chat history unless switched off."""
        return self.ui.chat_history.enabled and self.runtime.context.compaction.enabled is not False

    @model_validator(mode="after")
    def _plan_has_chat_history(self) -> AgentPayloadSpec:
        if self.runtime.context.plan.enabled and not self.ui.chat_history.enabled:
            raise ValueError("runtime.context.plan.enabled requires spec.ui.chat_history.enabled")
        return self

    def plan_enabled(self) -> bool:
        return self.runtime.context.plan.enabled

    @model_validator(mode="after")
    def _memory_has_chat_history(self) -> AgentPayloadSpec:
        if self.runtime.context.memory.enabled and not self.ui.chat_history.enabled:
            raise ValueError("runtime.context.memory.enabled requires spec.ui.chat_history.enabled")
        return self

    def memory_enabled(self) -> bool:
        return self.runtime.context.memory.enabled


class AgentPayloadConfig(BaseModel):
    """The ``payload:`` block when ``adapter: agent`` (typed spec)."""

    model_config = _SPEC_CONFIG

    adapter: str
    requires: list[PayloadBrickRequirement] = Field(default_factory=list)
    spec: AgentPayloadSpec = AgentPayloadSpec()


class AgentPayloadFile(BaseModel):
    """Top-level shape of the agent ``payload.yml`` (drives the schema)."""

    model_config = _SPEC_CONFIG

    payload: AgentPayloadConfig


__all__ = [
    "COMPACTION_TRIGGER_HEADROOM_TOKENS",
    "DEFAULT_CLEAR_ABOVE_INPUT_TOKENS",
    "DEFAULT_COMPACTION_TRIGGER_INPUT_TOKENS",
    "DEFAULT_SKILL_ROUTING_HEADER",
    "MAX_COMPACTION_INSTRUCTIONS_CHARS",
    "MAX_MEMORY_INDEX_ENTRIES",
    "SCHEMA_FQN_PATTERN",
    "SECURABLE_FQN_PATTERN",
    "A2AIntegration",
    "AgentCompactionRuntime",
    "AgentContextRuntime",
    "AgentDataUniverseRuntime",
    "AgentIntegrationsSpec",
    "AgentMcpRuntime",
    "AgentMemoryRuntime",
    "AgentModel",
    "AgentPayloadConfig",
    "AgentPayloadFile",
    "AgentPayloadSpec",
    "AgentPlanRuntime",
    "AgentPromptCacheRuntime",
    "AgentToolResultClearingRuntime",
    "AgentRuntimeSpec",
    "AgentSkillsRuntime",
    "AgentToolsRuntime",
    "AgentUISpec",
    "ChatHistoryUI",
    "ChatLayoutUI",
    "ChatWidth",
    "DiscoverySpec",
    "FeatureToggle",
    "FileUploadUI",
    "GatewayMcpRuntime",
    "GatewaySkillsRuntime",
    "Identity",
    "MCPServerIntegration",
    "McpDiscoverySpec",
    "McpLimits",
    "McpSource",
    "ModelServiceModel",
    "PinnedMcp",
    "PinnedSkill",
    "PinnedToolFilter",
    "ResourcesSidebarUI",
    "SchemaFqn",
    "SecurableFqn",
    "ServingEndpointModel",
    "SkillAuthoring",
    "SkillDiscoverySpec",
    "SkillLimits",
    "SkillsSource",
    "ToolOutputUI",
    "UserSkills",
]
