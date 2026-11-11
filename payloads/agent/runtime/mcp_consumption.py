"""
MCP server-side prompts and resources consumption (PR 4 / issue #110).

This module is the orchestration layer that sits between
:class:`payloads.agent.runtime.mcp_wrapper.FilteredMCPServerStreamableHttp` (which
holds the per-server brick-state config) and the agent runtime in
:mod:`payloads.agent.runtime.agent` (which knows the conversation id and owns the
request lifecycle).

Two public entry points:

* :func:`collect_prompt_manuals` -- fetch ``prompts/get`` for every
  server with ``prompts.enabled`` and return a single concatenated
  ``## Tool Manuals`` block ready to append to the ``system_prompt``.
* :func:`synthesize_resource_tools` -- one synthetic ``FunctionTool``
  per server with ``resources.enabled`` and ``expose_as: read_tool``;
  the LLM calls ``<prefix>read_resource(uri)`` and the tool routes
  through ``server.read_resource``.

Caching is intentionally split:

* ``per_request`` -- the caller (``managed_mcp_servers`` in
  :mod:`payloads.agent.runtime.mcp_connect`) owns a fresh ``dict`` per request and
  passes it in.  Lives exactly as long as the async-context.
* ``per_conversation`` -- a module-level bounded LRU keyed on
  ``(server_url, conversation_id)``.  Bound to 256 entries, which is
  large enough for typical fleet conversations and small enough that a
  long-running App process cannot OOM on prompt strings.
* ``per_process`` -- raised at config-load time by PR 1's
  :class:`agent_foundry.contracts.mcp.PromptsConfig` validator.  This
  module never sees the value.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import OrderedDict
from dataclasses import dataclass
from threading import Lock
from typing import TYPE_CHECKING, Any

from agents import FunctionTool

if TYPE_CHECKING:
    from payloads.agent.runtime.mcp_wrapper import FilteredMCPServerStreamableHttp

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# per_conversation cache (bounded LRU)
# ---------------------------------------------------------------------------
# 256 was picked for two reasons:
#   * The MLflow AgentServer process typically handles tens of concurrent
#     conversations; 256 leaves ~10x headroom before LRU eviction kicks
#     in for the longest-tailed sessions.
#   * At ~10 KB per cached prompt block, 256 entries -> ~2.5 MB worst
#     case, which is negligible compared to the LLM client buffers
#     already held in process.
# A configurable env-var knob is intentionally deferred; see the field
# guide for tuning guidance once production data exists.
_PER_CONVERSATION_CACHE_MAX = 256
_PER_CONVERSATION_CACHE: OrderedDict[tuple[str, str], "CachedManual"] = OrderedDict()
_CACHE_LOCK = Lock()


@dataclass(frozen=True)
class CachedManual:
    """Single server's contribution to the ``## Tool Manuals`` block.

    The dataclass is frozen so cached entries cannot be mutated after
    insertion, which keeps the LRU's bookkeeping correct under
    concurrent reads.  ``text`` carries the rendered Markdown for one
    server, e.g. ``### github: search_help\\n<body>\\n\\n### github:
    issue_template\\n<body>``.
    """

    text: str


def _cache_get(key: tuple[str, str]) -> CachedManual | None:
    """Return the cached entry for *key* and bump its LRU position."""
    with _CACHE_LOCK:
        entry = _PER_CONVERSATION_CACHE.get(key)
        if entry is not None:
            _PER_CONVERSATION_CACHE.move_to_end(key)
        return entry


def _cache_put(key: tuple[str, str], value: CachedManual) -> None:
    """Insert *value* under *key* and evict the oldest entry if full."""
    with _CACHE_LOCK:
        _PER_CONVERSATION_CACHE[key] = value
        _PER_CONVERSATION_CACHE.move_to_end(key)
        while len(_PER_CONVERSATION_CACHE) > _PER_CONVERSATION_CACHE_MAX:
            _PER_CONVERSATION_CACHE.popitem(last=False)


def _clear_per_conversation_cache() -> None:
    """Test-only escape hatch.  Production code never calls this."""
    with _CACHE_LOCK:
        _PER_CONVERSATION_CACHE.clear()


# ---------------------------------------------------------------------------
# Prompt manuals
# ---------------------------------------------------------------------------

_MANUALS_HEADER = "## Tool Manuals"


async def collect_prompt_manuals(
    servers: list["FilteredMCPServerStreamableHttp"],
    *,
    conversation_id: str | None,
    request_cache: dict[tuple[str, str], CachedManual],
) -> str:
    """Return the ``## Tool Manuals`` block for *servers*.

    Iterates *servers* in order; for every server whose ``prompts``
    config is enabled and has ``inject_into != "none"``, fetches each
    allow-listed prompt via :meth:`get_prompt` and concatenates the
    rendered text.  Servers that fail to reach
    ``list_prompts`` / ``get_prompt`` log at WARNING and contribute an
    empty entry -- the request continues without that server's manual.

    The ``cache:`` policy on each server's :class:`PromptsConfig`
    controls reuse:

    * ``per_request`` -- *request_cache* is the storage; the caller
      owns its lifetime.
    * ``per_conversation`` -- the module-level bounded LRU (with a
      ``per_request`` fallback when *conversation_id* is ``None``,
      e.g. local CLI runs or callers that don't supply a session id).

    Returns ``""`` when no server has anything to contribute, so the
    caller can append unconditionally.
    """
    if not servers:
        return ""

    sections: list[str] = []
    for server in servers:
        cfg = server.prompts
        if not cfg.enabled or cfg.inject_into == "none":
            continue
        if not cfg.allow:
            # Mirrors PR 1's validator soft-warning, but at runtime we
            # silently skip -- the deploy-time linter already nagged.
            continue

        cache_key = _resolve_cache_key(server, cfg.cache, conversation_id)
        cache_store = (
            request_cache
            if cfg.cache == "per_request" or conversation_id is None
            else None  # per_conversation uses the module-level LRU
        )

        cached = _lookup_cached_manual(cache_key, cache_store)
        if cached is not None:
            if cached.text:
                sections.append(cached.text)
            continue

        rendered = await _fetch_server_manual(server)
        cached_value = CachedManual(text=rendered)
        _store_cached_manual(cache_key, cached_value, cache_store)
        if rendered:
            sections.append(rendered)

    if not sections:
        return ""
    return "\n\n".join([_MANUALS_HEADER, *sections])


def _resolve_cache_key(
    server: "FilteredMCPServerStreamableHttp",
    cache_mode: str,
    conversation_id: str | None,
) -> tuple[str, str]:
    """Return the ``(server_url, scope_id)`` key for the cache lookup.

    The scope id is the conversation id for ``per_conversation`` mode,
    and the literal string ``"per_request"`` for the per-request fallback
    -- the latter is never used as a cross-request key because the
    request_cache dict is freshly created per request.
    """
    server_url = server.server_url or "unknown"
    if cache_mode == "per_conversation" and conversation_id:
        return (server_url, conversation_id)
    return (server_url, "per_request")


def _lookup_cached_manual(
    key: tuple[str, str],
    request_cache: dict[tuple[str, str], CachedManual] | None,
) -> CachedManual | None:
    """Look up *key* in either the request cache or the LRU."""
    if request_cache is not None:
        return request_cache.get(key)
    return _cache_get(key)


def _store_cached_manual(
    key: tuple[str, str],
    value: CachedManual,
    request_cache: dict[tuple[str, str], CachedManual] | None,
) -> None:
    """Store *value* under *key* in the appropriate cache."""
    if request_cache is not None:
        request_cache[key] = value
    else:
        _cache_put(key, value)


async def _fetch_server_manual(
    server: "FilteredMCPServerStreamableHttp",
) -> str:
    """Fetch every allow-listed prompt from *server* and render Markdown.

    Best-effort: a failure on either ``list_prompts`` or any individual
    ``get_prompt`` is logged at WARNING and skipped.  An empty return
    value means "nothing to inject for this server" and is cached so
    the next request in the same scope skips the round-trip too.
    """
    cfg = server.prompts
    server_label = server.name

    try:
        listed = await server.list_prompts()
    except Exception as exc:
        logger.warning(
            "[mcp] %s: list_prompts failed -- skipping manuals (%s)",
            server_label,
            exc,
        )
        return ""

    available = {p.name for p in getattr(listed, "prompts", [])}

    # ``["*"]`` is the documented "all prompts" sentinel from PR 1's
    # PromptsConfig contract; expand it here so downstream logic stays
    # uniform.
    if cfg.allow == ["*"]:
        wanted = sorted(available)
    else:
        wanted = [name for name in cfg.allow if name in available]

    if not wanted:
        return ""

    rendered_parts: list[str] = []
    for name in wanted:
        body = await _fetch_one_prompt(server, name)
        if body:
            rendered_parts.append(f"### {server_label}: {name}\n{body}")

    return "\n\n".join(rendered_parts)


async def _fetch_one_prompt(
    server: "FilteredMCPServerStreamableHttp",
    name: str,
) -> str:
    """Fetch ``prompts/get`` for *name* and flatten messages to text.

    Per the PR 4 spec, v1 calls ``get_prompt`` with no arguments.  MCP
    servers that return a parameterised template will surface the
    unfilled placeholders -- the LLM still benefits from seeing the
    structure even when slots aren't pre-populated.
    """
    try:
        result = await server.get_prompt(name)
    except Exception as exc:
        logger.warning(
            "[mcp] %s: get_prompt(%r) failed -- skipping (%s)",
            server.name,
            name,
            exc,
        )
        return ""

    pieces: list[str] = []
    for message in getattr(result, "messages", []):
        text = _flatten_prompt_message(message)
        if text:
            pieces.append(text)
    return "\n\n".join(pieces)


def _flatten_prompt_message(message: Any) -> str:
    """Extract plain text from a ``PromptMessage``.

    The MCP type allows ``content`` to be either a single content block
    or a list; each block may be a ``TextContent``, ``ImageContent``,
    or ``EmbeddedResource``.  We surface text only -- image / embedded
    resource bodies are too lossy to drop into a system prompt and the
    LLM has no way to render them anyway.
    """
    content = getattr(message, "content", None)
    if content is None:
        return ""
    parts: list[str] = []
    items = content if isinstance(content, list) else [content]
    for item in items:
        text = getattr(item, "text", None)
        if isinstance(text, str) and text:
            parts.append(text)
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Synthetic <prefix>read_resource tool
# ---------------------------------------------------------------------------


def synthesize_resource_tools(
    servers: list["FilteredMCPServerStreamableHttp"],
) -> list[FunctionTool]:
    """One synthetic ``FunctionTool`` per server with resources enabled.

    The tool's name follows the brick's ``name_prefix`` (e.g.
    ``github__read_resource``) so it sorts naturally next to the
    server's MCP-served tools and cannot collide with framework-internal
    single-leading-underscore tools (see field-guide section on the
    naming convention).

    When ``expose_as`` is anything other than ``"read_tool"`` (today the
    only other value is ``"none"``) the server is skipped silently.
    """
    tools: list[FunctionTool] = []
    for server in servers:
        cfg = server.resources
        if not cfg.enabled or cfg.expose_as != "read_tool" or not cfg.allow:
            continue
        tools.append(_make_read_resource_tool(server))
    return tools


def _make_read_resource_tool(
    server: "FilteredMCPServerStreamableHttp",
) -> FunctionTool:
    """Build the ``<prefix>read_resource`` FunctionTool for *server*.

    The tool description enumerates the allow-listed URI patterns so
    the LLM can pick a real one rather than guessing.  Pattern matching
    on actual call uses :meth:`FilteredMCPServerStreamableHttp.matches_resource_uri`
    -- a request for a non-allowed URI returns a structured error
    string, never an exception, to keep the SDK's tool-call pipeline
    unaffected.
    """
    name = f"{server.name_prefix}read_resource"
    cfg = server.resources

    description = (
        f"Read the contents of a Model Context Protocol resource "
        f"hosted on the {server.name!r} MCP server. "
        "Pass an exact URI matching one of the allow-listed entries: "
        f"{', '.join(cfg.allow)}. "
        "Returns the resource body as text."
    )

    async def _invoke(_ctx: Any, arguments_json: str) -> str:
        return await _read_resource_invoke(server, arguments_json)

    return FunctionTool(
        name=name,
        description=description,
        params_json_schema={
            "type": "object",
            "properties": {
                "uri": {
                    "type": "string",
                    "description": ("The URI of the resource to read.  Must match one of the allow-listed patterns."),
                },
            },
            "required": ["uri"],
        },
        on_invoke_tool=_invoke,
        strict_json_schema=False,
    )


async def _read_resource_invoke(
    server: "FilteredMCPServerStreamableHttp",
    arguments_json: str,
) -> str:
    """Resolve, allow-check, and dispatch a single read_resource call.

    Three failure modes are handled here -- each returns a textual
    explanation rather than raising, because raising would surface as
    an opaque tool-error to the LLM:

    1. Malformed JSON arguments -> structured error.
    2. URI not allow-listed -> structured error listing the allowed
       patterns so the LLM can self-correct.
    3. Upstream ``read_resource`` raises -> structured error with the
       exception message redacted to ``{type}: {short_msg}`` to avoid
       leaking server internals.
    """
    try:
        args = json.loads(arguments_json) if arguments_json else {}
    except json.JSONDecodeError:
        return "Error: arguments must be valid JSON with a 'uri' string field."

    uri = args.get("uri")
    if not isinstance(uri, str) or not uri:
        return "Error: missing 'uri' string argument."

    if not server.matches_resource_uri(uri):
        allowed = ", ".join(server.resources.allow) or "(none)"
        return f"Error: URI {uri!r} is not in the allow-list for server {server.name!r}. Allowed patterns: {allowed}."

    try:
        result = await server.read_resource(uri)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return f"Error reading {uri!r}: {type(exc).__name__}: {exc}"

    return _flatten_resource_contents(result)


def _flatten_resource_contents(result: Any) -> str:
    """Reduce a ``ReadResourceResult`` to plain text.

    Mirrors :func:`_flatten_prompt_message` -- text-only.  Binary
    resources (``BlobResourceContents``) are summarised by length
    instead of base64-decoding into the prompt, which would burn tokens
    on data the LLM cannot interpret.
    """
    contents = getattr(result, "contents", None) or []
    parts: list[str] = []
    for item in contents:
        text = getattr(item, "text", None)
        if isinstance(text, str) and text:
            parts.append(text)
            continue
        blob = getattr(item, "blob", None)
        if blob:
            mime = getattr(item, "mimeType", "application/octet-stream")
            parts.append(f"(binary content, {len(blob)} bytes, {mime})")
    return "\n\n".join(parts) if parts else "(empty resource)"
