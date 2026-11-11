"""
MCP server wrapper -- allow/alias/prefix, prompts/resources, auto-poll
======================================================================

Adds four concerns on top of the openai-agents SDK's
:class:`agents.mcp.MCPServerStreamableHttp`:

1. **Allow / glob / alias** (``allow_entries``) -- per-server filter on
   upstream tool names plus optional rename.  Each entry's ``match``
   field is an :mod:`fnmatch`-style pattern (literal ``foo`` is just
   an exact match) and ``expose_as`` collapses any matched upstream
   name to a stable alias before prefixing.  This replaces the SDK's
   ``tool_filter`` (which was exact-match-only and could not rename),
   and it is the only place where allow-listing is enforced.
2. **Tool name prefixing** (``name_prefix``) -- per-server prefixing of
   the (possibly aliased) tool names so two MCP servers can publish
   ``query`` without colliding in the LLM-visible toolset.
3. **Prompts / resources surface** (``prompts``, ``resources``,
   ``server_url``) -- snapshotted brick-state config used by
   :mod:`payloads.agent.runtime.mcp_consumption` to fetch prompt manuals and
   synthesise a ``<prefix>read_resource`` tool per server.  The
   wrapper only carries the config and a URI matcher; the orchestration
   (caching, fetch, error handling) lives in the consumption module.
4. **Auto-poll** (``auto_poll``) -- when set, ``call_tool`` for the
   trigger tool absorbs an async ``submit -> poll`` cycle internally:
   parse the submit response, extract the receipt IDs via
   ``forward_args``, loop ``poll_with`` calls with exponential backoff
   until the upstream's state field exits ``pending_states``, and
   return the terminal poll result as if it were the trigger's direct
   response.  The poll tool is hidden from :meth:`list_tools` (so the
   LLM cannot call it directly).  As a defensive safety net,
   ``call_tool`` *also* short-circuits any LLM-issued direct call to
   the configured ``poll_with`` tool: chat UIs cache tool definitions
   across deploys and the model can hallucinate poll calls from a
   still-pending payload.  A WARNING log line marks every such
   occurrence so we can audit if ``hide_poll_tool`` ever leaks back
   to the LLM in the wild.  See
   :class:`agent_foundry.contracts.mcp.AutoPollConfig` for the
   per-type defaults that ship with managed_sql / managed_genie.
5. **SQL result compacting** -- after auto-poll, successful
   ``execute_sql*`` results are rewritten to the compact
   ``{columns, rows, total_rows, truncated}`` shape so the
   model and Lakebase history do not carry Statement Execution
   type metadata / value wrappers.

Per-call ``_meta`` injection (``tools.default_meta``) is still
configured at construction time via the SDK's ``tool_meta_resolver``
argument (see :func:`payloads.agent.runtime.brick_mcp_servers.build_mcp_servers`).

Design rationale
----------------
The openai-agents SDK (``>=0.13.2``) supports::

    server = MCPServerStreamableHttp(
        params=...,
        tool_filter=create_static_tool_filter(allowed_tool_names=...),
        tool_meta_resolver=lambda ctx: {...},
    )

What the SDK does *not* do is:

- Glob matching on upstream tool names.  Required for managed Genie
  servers whose tools embed the space UUID (``query_space_<uuid>``).
- Renaming upstream tools to a stable alias.  Required for managed
  Functions / Vector Search servers whose upstream names embed the
  catalog and schema (``<cat>__<sch>__<func>``).
- Server-level tool **name prefixing**.  Re-implemented here as a
  ``list_tools`` alias-and-prefix step + ``call_tool`` reverse-lookup
  round-trip.
"""

from __future__ import annotations

import asyncio
import fnmatch
import inspect
import json
import logging
from typing import TYPE_CHECKING, Any

import mlflow
from agents.mcp import MCPServerStreamableHttp, MCPToolMetaContext

from agent_foundry.contracts.mcp import (
    AutoPollConfig,
    PromptsConfig,
    ResourcesConfig,
    ToolAllowEntry,
)
from payloads.agent.runtime.mcp_tools_cache import (
    CachedTools,
    CacheKey,
    forget_tools,
    lookup_tools,
    store_tools,
)
from payloads.agent.runtime.sql_result_compact import (
    compact_call_tool_result,
    is_sql_execute_upstream,
)

if TYPE_CHECKING:
    from agents import AgentBase
    from agents.mcp import MCPTool
    from agents.run_context import RunContextWrapper
    from mcp.types import CallToolResult

logger = logging.getLogger(__name__)


class FilteredMCPServerStreamableHttp(MCPServerStreamableHttp):
    """``MCPServerStreamableHttp`` subclass that allow-lists, aliases,
    and prefixes upstream tool names.

    The original (un-aliased, un-prefixed) name is stored per instance
    so :meth:`call_tool` can reverse the mapping when forwarding the
    JSON-RPC ``tools/call`` request.  The mapping is rebuilt on every
    :meth:`list_tools` invocation, which keeps aliases honest when the
    upstream tool set changes (e.g. after ``invalidate_tools_cache()``).

    ``name_prefix`` is intentionally a plain string, not a callable,
    because per-server prefixes are static deploy-time configuration
    (``server.yml -> tools.name_prefix``).  An empty prefix is a no-op
    so this class is safe to use even when prefixing is disabled.

    ``allow_entries`` carries the per-server ``tools.allow`` list as
    normalised :class:`ToolAllowEntry` objects.  When ``None`` the
    wrapper performs no filtering or aliasing -- every upstream tool
    is admitted and visible as ``<prefix><upstream_name>``.  This is
    the legacy behaviour preserved so callers that opt out of
    aliasing (e.g. unit tests, hand-built wrappers) continue to work.
    """

    def __init__(
        self,
        *args: Any,
        name_prefix: str = "",
        allow_entries: list[ToolAllowEntry] | None = None,
        prompts: PromptsConfig | None = None,
        resources: ResourcesConfig | None = None,
        auto_poll: AutoPollConfig | None = None,
        server_url: str = "",
        cache_scope: str = "",
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._name_prefix = name_prefix
        # Identity scope for the process-wide tools-list cache (see
        # :mod:`payloads.agent.runtime.mcp_tools_cache`).  Empty
        # disables it.
        self._cache_scope = cache_scope
        # Background connect started by
        # :func:`payloads.agent.runtime.mcp_deferred_connect.deferred_connect`;
        # ``None`` when the caller connected up front.
        self._pending_connect: asyncio.Future[Any] | None = None
        self._allow_entries = list(allow_entries) if allow_entries is not None else None
        # Snapshot the brick-state prompts/resources config so the
        # consumption module can decide whether to fetch and how to
        # cache without re-loading state.  Defaults are
        # ``enabled=False`` -- a server with no opt-in stays inert at
        # runtime even when discovered by ``inspect``.
        self._prompts = prompts or PromptsConfig()
        self._resources = resources or ResourcesConfig()
        # ``None`` means auto-poll is disabled for this server.  When
        # set, :meth:`call_tool` for the trigger absorbs the async
        # submit -> poll cycle internally and the LLM sees one tool
        # call instead of two.  See :class:`AutoPollConfig` and
        # :meth:`_auto_poll_loop` for behaviour.
        self._auto_poll = auto_poll
        # Cache keys for ``per_conversation`` / ``per_request`` modes
        # (see ``bricks/agent/mcp_consumption.py``) need a stable URL,
        # which is *not* exposed by the SDK base.  We snapshot it here.
        self._server_url = server_url
        # Maps the LLM-visible name (alias + prefix applied) back to the
        # upstream name that ``call_tool`` must forward.  Rebuilt on
        # every ``list_tools`` to stay in sync with upstream churn.
        self._prefixed_to_original: dict[str, str] = {}

    @property
    def name_prefix(self) -> str:
        """Return the configured tool-name prefix (read-only)."""
        return self._name_prefix

    @property
    def allow_entries(self) -> list[ToolAllowEntry] | None:
        """Return the allow-list entries (read-only).

        ``None`` means "no filtering" (legacy behaviour for callers
        that opt out of allow-listing entirely).  Returns a *copy* so
        callers cannot mutate the wrapper's internal list.
        """
        if self._allow_entries is None:
            return None
        return list(self._allow_entries)

    @property
    def prompts(self) -> PromptsConfig:
        """Return the configured prompts policy (read-only)."""
        return self._prompts

    @property
    def resources(self) -> ResourcesConfig:
        """Return the configured resources policy (read-only)."""
        return self._resources

    @property
    def auto_poll(self) -> AutoPollConfig | None:
        """Return the configured auto-poll policy (read-only).

        ``None`` means auto-polling is off for this server -- the LLM
        is responsible for chaining submit and poll calls itself.
        """
        return self._auto_poll

    @property
    def server_url(self) -> str:
        """Return the upstream MCP server URL (read-only).

        Used as the first element of cache keys in
        :mod:`payloads.agent.runtime.mcp_consumption`; falls back to the SDK base
        ``params['url']`` when the constructor caller did not provide
        one explicitly.  Returning an empty string here disables
        prompt/resource caching for this instance entirely.
        """
        return self._server_url

    def matches_resource_uri(self, uri: str) -> bool:
        """Return True when *uri* is allowed by ``resources.allow``.

        Supports both exact match and ``fnmatch``-style globs (per the
        PR 1 contract: "exact URIs *or* glob patterns").  The matcher
        is case-sensitive because most MCP resource URIs are
        ``scheme://host/path`` style where case matters.

        Returns False when ``resources.enabled`` is False so callers
        do not need a separate guard before invoking
        :meth:`read_resource`.
        """
        if not self._resources.enabled:
            return False
        return any(uri == pattern or fnmatch.fnmatchcase(uri, pattern) for pattern in self._resources.allow)

    def _tools_cache_key(self) -> CacheKey | None:
        if not (self._server_url and self._cache_scope):
            return None
        return (self._server_url, self._cache_scope)

    def has_cached_tools(self) -> bool:
        """Whether :meth:`list_tools` can be answered without a session."""
        key = self._tools_cache_key()
        return key is not None and lookup_tools(key) is not None

    def forget_cached_tools(self) -> None:
        """Drop this server's process-cached tool list, so the next request connects before the run."""
        key = self._tools_cache_key()
        if key is not None:
            forget_tools(key)

    def defer_connect(self, pending: "asyncio.Future[Any]") -> None:
        """Register an in-flight background connect that tool calls await."""
        self._pending_connect = pending

    async def connect(self) -> None:
        """Open the session and run the MCP ``initialize`` handshake, traced per server."""
        with mlflow.start_span(name=f"mcp.connect[{self.name}]", span_type="TASK"):
            await super().connect()  # type: ignore[no-untyped-call]

    async def refresh_stale_tools(self) -> None:
        """Refetch a stale cached tool list over this server's open session."""
        key = self._tools_cache_key()
        cached = lookup_tools(key) if key else None
        if cached is None or not cached.stale:
            return
        with mlflow.start_span(name=f"mcp.refresh_tools[{self.name}]", span_type="TASK"):
            self._cache_dirty = True
            await self._fetch_upstream_tools(None, None)

    def _session_ready(self) -> bool:
        pending = self._pending_connect
        return self.session is not None and (pending is None or pending.done())

    async def _await_pending_connect(self) -> None:
        # Shielded: one cancelled tool call must not cancel the connect
        # every other call on this request is waiting for.
        if self._pending_connect is not None and not self._pending_connect.done():
            await asyncio.shield(self._pending_connect)

    async def _list_upstream_tools(
        self,
        run_context: "RunContextWrapper[Any] | None",
        agent: "AgentBase | None",
    ) -> "tuple[list[MCPTool], CachedTools | None]":
        """Fetch upstream tools, backed by the process cache.

        A hit is served without a session, so it works while a deferred
        connect is still in flight.  It seeds the SDK's per-instance cache
        (``_tools_list`` / ``_cache_dirty``, openai-agents 0.15) because
        ``call_tool`` validates required parameters against ``_tools_list``.
        The SDK ``tool_filter`` is not applied on a hit; brick servers use
        ``allow_entries`` instead.  A stale hit is served only while the
        session is not ready yet; otherwise it is refetched.  Returns the
        tools and the cache hit served, ``None`` when fetched upstream.
        """
        key = self._tools_cache_key()
        cached = lookup_tools(key) if key else None
        if cached is not None:
            if not (cached.stale and self._session_ready()):
                self._tools_list = cached.tools
                self._cache_dirty = False
                return cached.tools, cached
            self._cache_dirty = True
        await self._await_pending_connect()
        return await self._fetch_upstream_tools(run_context, agent), None

    async def _fetch_upstream_tools(
        self,
        run_context: "RunContextWrapper[Any] | None",
        agent: "AgentBase | None",
    ) -> "list[MCPTool]":
        """List tools via the SDK base and store them in the process cache."""
        tools = await super().list_tools(run_context=run_context, agent=agent)
        key = self._tools_cache_key()
        if key and self._tools_list:
            store_tools(key, self._tools_list)
        return tools

    async def list_tools(
        self,
        run_context: "RunContextWrapper[Any] | None" = None,
        agent: "AgentBase | None" = None,
    ) -> "list[MCPTool]":
        """Return the allow-listed, aliased, and prefixed tool list.

        Pipeline (top -> bottom):

        1. Fetch every upstream tool via the SDK base.
        2. If ``allow_entries`` is set, drop tools whose name does not
           match any entry's ``match`` glob.  When unset, admit all.
        3. For each kept tool, compute the alias = ``expose_as`` (when
           the matching entry sets it) or the upstream name verbatim.
        4. Concat ``<name_prefix><alias>`` to produce the LLM-visible
           name.

        Wrapped in an ``mcp.list_tools[<server>]`` MLflow span so the
        per-server cost is visible in the trace -- without it the
        SDK's anonymous ``mcp_tools_span`` would surface to MLflow as
        an unnamed ``Unknown`` bar.
        """
        with mlflow.start_span(
            name=f"mcp.list_tools[{self.name}]",
            span_type="TASK",
        ) as span:
            tools, cached = await self._list_upstream_tools(run_context, agent)
            span.set_attribute("upstream_count", len(tools))
            span.set_attribute("cache_hit", cached is not None)
            if cached is not None and cached.stale:
                span.set_attribute("stale", True)

        # Fast path: no allow-listing and no prefixing -> upstream
        # list passes through verbatim, mapping stays empty.  Equivalent
        # to the SDK base's behaviour and preserves identity for tests
        # that assert ``result is upstream``.
        if self._allow_entries is None and not self._name_prefix:
            self._prefixed_to_original = {}
            return tools

        # Apply allow / alias / prefix.  Each ``ToolAllowEntry``
        # contributes at most one tool per matched upstream name; the
        # contract validator already rejected duplicate aliases so the
        # mapping is guaranteed injective.
        kept: list["MCPTool"] = []
        new_mapping: dict[str, str] = {}
        for tool in tools:
            alias = self._resolve_alias(tool.name)
            if alias is None:
                continue
            visible = f"{self._name_prefix}{alias}"
            new_mapping[visible] = tool.name
            kept.append(tool.model_copy(update={"name": visible}))

        # ``self._prefixed_to_original`` carries BOTH the visible-tool
        # mapping and the auto-poll target's mapping.  We must build it
        # before the ``hide_poll_tool`` filter below removes the poll
        # tool from the LLM-visible list -- otherwise ``call_tool``'s
        # internal poll dispatch loses the alias->upstream lookup.
        self._prefixed_to_original = new_mapping

        if self._auto_poll is not None and self._auto_poll.hide_poll_tool:
            poll_visible = f"{self._name_prefix}{self._auto_poll.poll_with}"
            kept = [t for t in kept if t.name != poll_visible]

        return kept

    def _resolve_alias(self, upstream_name: str) -> str | None:
        """Return the alias for *upstream_name* or None if it's filtered.

        When ``allow_entries`` is unset every upstream name is admitted
        verbatim (alias = upstream).  Otherwise the first entry whose
        ``match`` glob accepts *upstream_name* wins; an entry without
        ``expose_as`` keeps the upstream name as the alias.
        """
        if self._allow_entries is None:
            return upstream_name
        for entry in self._allow_entries:
            if fnmatch.fnmatchcase(upstream_name, entry.match):
                return entry.expose_as or upstream_name
        return None

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None,
        meta: dict[str, Any] | None = None,
    ) -> "CallToolResult":
        """Strip prefix and forward the call to the upstream server.

        ``meta`` is passed through unchanged.  The SDK's
        ``tool_meta_resolver`` (configured at construction time) is
        invoked separately by ``MCPUtil.invoke_mcp_tool`` *before* this
        method is called, so ``meta`` here already contains the merged
        resolver output plus any LLM-supplied keys.

        Wrapped in an ``mcp.call_tool[<server>:<tool>]`` MLflow span so
        per-tool latency (including auto-poll absorption when
        configured) shows up named in traces instead of as ``Unknown``.
        """
        with mlflow.start_span(
            name=f"mcp.call_tool[{self.name}:{tool_name}]",
            span_type="TASK",
        ) as span:
            await self._await_pending_connect()
            return await self._call_tool_impl(tool_name, arguments, meta, span)

    async def _call_tool_impl(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None,
        meta: dict[str, Any] | None,
        span: Any,
    ) -> "CallToolResult":
        """Original ``call_tool`` body, factored out so the public
        method can wrap it in a single MLflow span without nesting
        the auto-poll loop's internals."""
        result = await self._forward(tool_name, arguments, meta, span)
        original = self._prefixed_to_original.get(tool_name, tool_name)

        # Compact successful SQL envelopes after auto-poll so the LLM
        # and Lakebase see the canonical table shape. Match
        # on the *upstream* name so expose_as renames cannot disable it.
        if is_sql_execute_upstream(original):
            if compact_call_tool_result(result):
                logger.debug(
                    "[mcp_wrapper] %s: compacted SQL tool result for %s",
                    self.name,
                    original,
                )

        return result

    def visible_name(self, upstream_name: str) -> str | None:
        """The LLM-visible name of *upstream_name*, or ``None`` when the allow-list filters it out."""
        alias = self._resolve_alias(upstream_name)
        return None if alias is None else f"{self._name_prefix}{alias}"

    async def call_tool_unbounded(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        run_context: "RunContextWrapper[Any]",
    ) -> "CallToolResult":
        """:meth:`call_tool` without the SQL result bounds, for reading slices of the full result (#688)."""
        with mlflow.start_span(
            name=f"mcp.call_tool_unbounded[{self.name}:{tool_name}]",
            span_type="TASK",
        ) as span:
            await self._await_pending_connect()
            meta = await self._resolve_meta(tool_name, arguments, run_context)
            return await self._forward(tool_name, arguments, meta, span)

    async def _resolve_meta(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        run_context: "RunContextWrapper[Any]",
    ) -> dict[str, Any] | None:
        """The ``_meta`` the SDK would send for this call (``tools.default_meta``)."""
        if self.tool_meta_resolver is None:
            return None
        context = MCPToolMetaContext(
            run_context=run_context, server_name=self.name, tool_name=tool_name, arguments=dict(arguments)
        )
        meta = self.tool_meta_resolver(context)
        return await meta if inspect.isawaitable(meta) else meta

    async def _forward(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None,
        meta: dict[str, Any] | None,
        span: Any,
    ) -> "CallToolResult":
        """Forward the call upstream, absorbing auto-poll; the result is not reshaped."""
        original = self._prefixed_to_original.get(tool_name, tool_name)
        span.set_attribute("upstream_tool_name", original)
        # Stable tool-type tag for downstream scorers (e.g.
        # ``genie_sql_correctness``) that must discover Genie spans
        # without depending on operator-chosen ``name_prefix`` values.
        # The ``query_space*`` upstream-name shape is fixed by the
        # ``managed_genie`` server contract (see
        # ``agent_foundry/contracts/mcp.py``).
        if fnmatch.fnmatchcase(original, "query_space*"):
            span.set_attribute("tool_type", "genie")

        # Defensive short-circuit for the configured poll tool.  The LLM
        # should never call the poll tool directly when ``hide_poll_tool``
        # is true (it isn't in :meth:`list_tools`), but Databricks Apps
        # / chat UIs cache tool definitions across deploys and the
        # model may also hallucinate the call from a still-pending
        # response payload.  Forward the call directly so we don't
        # recurse into ``_auto_poll_loop`` (the poll tool *is* the loop).
        if self._auto_poll is not None:
            poll_visible = f"{self._name_prefix}{self._auto_poll.poll_with}"
            if tool_name == poll_visible:
                if self._auto_poll.hide_poll_tool:
                    logger.warning(
                        "[mcp_wrapper] %s: LLM called hidden poll tool %s "
                        "directly (auto_poll.hide_poll_tool=true); "
                        "forwarding upstream",
                        self.name,
                        tool_name,
                    )
                if original != tool_name:
                    logger.debug(
                        "[mcp_wrapper] %s: %s -> %s (poll bypass)",
                        self.name,
                        tool_name,
                        original,
                    )
                return await super().call_tool(original, arguments, meta=meta)

        if original != tool_name:
            logger.debug(
                "[mcp_wrapper] %s: %s -> %s",
                self.name,
                tool_name,
                original,
            )
        result = await super().call_tool(original, arguments, meta=meta)

        # Auto-poll absorption: if this server has auto_poll configured
        # and the LLM just called the trigger tool, run the poll loop
        # internally so the LLM sees ONE call+response (terminal poll
        # result) instead of N tool cells in the chat UI.  Internal
        # poll calls go through ``super().call_tool`` directly to avoid
        # ``call_tool`` recursion (no second auto-poll trigger).
        if self._auto_poll is not None:
            trigger_visible = f"{self._name_prefix}{self._auto_poll.trigger}"
            if tool_name == trigger_visible:
                result = await self._auto_poll_loop(result, meta)
        return result

    async def _auto_poll_loop(
        self,
        initial_result: "CallToolResult",
        meta: dict[str, Any] | None,
    ) -> "CallToolResult":
        """Repeatedly poll the upstream until terminal state.

        Fail-soft on every shape mismatch: if the submit response
        cannot be parsed as JSON, lacks the configured ``state_field``,
        or is missing a ``forward_args`` source field, the wrapper
        logs a warning and returns ``initial_result`` unchanged.  This
        guarantees the LLM always gets a response -- if Databricks
        evolves the upstream JSON envelope, the worst case is the LLM
        sees the original "PENDING" payload and chains the poll itself.
        """
        cfg = self._auto_poll
        assert cfg is not None  # caller-guarded; satisfies type checker

        payload = self._parse_text_payload(initial_result)
        if payload is None:
            logger.debug(
                "[auto_poll] %s: trigger response is not JSON-parseable; skipping poll",
                self.name,
            )
            return initial_result

        if not self._matches_pending(payload, cfg):
            # Already terminal on the first call -- e.g. a small SQL
            # query that completes within the upstream's initial
            # ``wait_timeout``.  No polling needed.
            return initial_result

        poll_args = self._extract_poll_args(payload, cfg)
        if poll_args is None:
            logger.warning(
                "[auto_poll] %s: trigger response missing forward_args "
                "fields %s; returning unpolled response so the LLM can "
                "fall back to manual chaining",
                self.name,
                [fwd.from_field for fwd in cfg.forward_args],
            )
            return initial_result

        poll_visible = f"{self._name_prefix}{cfg.poll_with}"
        poll_original = self._prefixed_to_original.get(poll_visible, cfg.poll_with)

        loop = asyncio.get_event_loop()
        deadline = loop.time() + cfg.timeout_s
        wait = cfg.initial_interval_s
        last_result = initial_result

        while True:
            await asyncio.sleep(wait)
            if loop.time() >= deadline:
                logger.warning(
                    "[auto_poll] %s: timeout after %.1fs polling %s; returning last seen pending result",
                    self.name,
                    cfg.timeout_s,
                    poll_original,
                )
                return last_result

            last_result = await super().call_tool(
                poll_original,
                poll_args,
                meta=meta,
            )
            poll_payload = self._parse_text_payload(last_result)
            if poll_payload is None:
                # Poll returned a non-JSON envelope (rare; usually means
                # the upstream errored out).  Treat as terminal and
                # return verbatim so the LLM can surface it.
                return last_result
            if not self._matches_pending(poll_payload, cfg):
                logger.debug(
                    "[auto_poll] %s: poll terminal after %.1fs",
                    self.name,
                    loop.time() - (deadline - cfg.timeout_s),
                )
                return last_result

            wait = min(wait * cfg.backoff_factor, cfg.max_interval_s)

    @staticmethod
    def _parse_text_payload(result: "CallToolResult") -> dict[str, Any] | None:
        """Extract a JSON object from the first text-content item.

        MCP tool results are a list of typed content items; managed
        Databricks servers (SQL / Genie) emit a single ``TextContent``
        whose ``text`` is a JSON-encoded receipt.  We probe each item
        in order for compatibility with future content types.
        """
        for content in result.content or []:
            text = getattr(content, "text", None)
            if not text:
                continue
            try:
                parsed = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(parsed, dict):
                return parsed
        return None

    @staticmethod
    def _matches_pending(payload: dict[str, Any], cfg: AutoPollConfig) -> bool:
        """True if ``payload[state_field]`` is one of ``cfg.pending_states``."""
        value = _get_dotted(payload, cfg.state_field)
        if value is None:
            return False
        return value in cfg.pending_states

    @staticmethod
    def _extract_poll_args(
        payload: dict[str, Any],
        cfg: AutoPollConfig,
    ) -> dict[str, Any] | None:
        """Build the poll-call kwargs by pulling each ``from_field``.

        Returns ``None`` when any required source field is missing so
        the caller can fail soft (return the original response, log a
        warning, and let the LLM chain manually).
        """
        args: dict[str, Any] = {}
        for fwd in cfg.forward_args:
            value = _get_dotted(payload, fwd.from_field)
            if value is None:
                return None
            args[fwd.to_arg] = value
        return args


def _get_dotted(obj: Any, path: str) -> Any:
    """Look up ``path`` in ``obj`` using a dotted key chain.

    Returns ``None`` when any intermediate key is missing or the
    intermediate value is not a dict.  Single-segment paths reduce to
    a flat dict lookup.  Used by the auto-poll loop for both the state
    probe and the forward-args extraction so the two stay symmetric.
    """
    cursor: Any = obj
    for key in path.split("."):
        if not isinstance(cursor, dict) or key not in cursor:
            return None
        cursor = cursor[key]
    return cursor
