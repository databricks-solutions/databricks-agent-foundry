"""Process-wide cache of upstream MCP ``tools/list`` results.

MCP server wrappers are rebuilt on every request (the OAuth provider
closes over the request's token), so the SDK's per-instance
``cache_tools_list`` never survives past one turn.  Production traces
showed the sequential ``list_tools`` pass costing ~1-1.5s before the
first model call on *every* turn; this module amortises it across
requests.

Entries are keyed by ``(server_url, identity_scope)`` so one caller's
tool list is never served to another: upstream tool visibility can
depend on the caller's permissions.  Use :func:`identity_scope` to
derive the scope -- it never stores the raw token.

An entry is fresh for the TTL and then *stale* until :data:`_MAX_AGE_SECONDS`:
a stale list is still served, so the request can start before its servers
connect, and the server refreshes it once connected.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agent_foundry import env

if TYPE_CHECKING:
    from agents.mcp import MCPServer, MCPTool

logger = logging.getLogger(__name__)

CacheKey = tuple[str, str]

SERVICE_PRINCIPAL_SCOPE = "sp"
_TTL_SECONDS_DEFAULT = 300.0
_MAX_AGE_SECONDS = 3600.0
_MAX_ENTRIES = 256


@dataclass(frozen=True)
class CachedTools:
    """A cached upstream tool list; *stale* once past the TTL."""

    tools: list[MCPTool]
    stale: bool


@dataclass(frozen=True)
class _Entry:
    tools: list[MCPTool]
    fresh_until: float
    expires_at: float


_CACHE: OrderedDict[CacheKey, _Entry] = OrderedDict()


def identity_scope(obo_token: str | None) -> str:
    """Return the cache scope for a caller: a token digest, or the SP scope."""
    if not obo_token:
        return SERVICE_PRINCIPAL_SCOPE
    return hashlib.sha256(obo_token.encode("utf-8")).hexdigest()


def ttl_seconds() -> float:
    """Resolve the entry lifetime from ``MCP_TOOLS_CACHE_TTL_SECONDS``.

    ``0`` disables the cache; unset, malformed or negative values fall
    back to the default.
    """
    raw = env.read_optional(env.MCP_TOOLS_CACHE_TTL_SECONDS) or ""
    try:
        value = float(raw)
    except ValueError:
        return _TTL_SECONDS_DEFAULT
    return value if value >= 0 else _TTL_SECONDS_DEFAULT


def lookup_tools(key: CacheKey) -> CachedTools | None:
    """Return the cached upstream tool list, or ``None`` on miss or past the max age."""
    entry = _CACHE.get(key)
    if entry is None:
        return None
    now = time.monotonic()
    if entry.expires_at <= now:
        del _CACHE[key]
        return None
    return CachedTools(tools=list(entry.tools), stale=entry.fresh_until <= now)


def store_tools(key: CacheKey, tools: list[MCPTool]) -> None:
    """Cache *tools* under *key*, evicting the oldest entry when full."""
    ttl = ttl_seconds()
    if ttl == 0:
        return
    now = time.monotonic()
    _CACHE.pop(key, None)
    _CACHE[key] = _Entry(tools=list(tools), fresh_until=now + ttl, expires_at=now + max(ttl, _MAX_AGE_SECONDS))
    while len(_CACHE) > _MAX_ENTRIES:
        _CACHE.popitem(last=False)


def forget_tools(key: CacheKey) -> None:
    """Drop the cached tool list for *key*, if any."""
    _CACHE.pop(key, None)


async def prewarm_tool_lists(servers: Sequence[MCPServer]) -> None:
    """List every connected server's tools concurrently.

    The SDK lists servers one after another at run start; doing it here
    in parallel fills each server's ``cache_tools_list`` so that pass is
    free.  Failures are logged only -- the SDK retries during the run.
    """
    results = await asyncio.gather(*(server.list_tools() for server in servers), return_exceptions=True)
    for server, result in zip(servers, results, strict=True):
        if isinstance(result, Exception):
            logger.warning("[mcp] tools prewarm failed for %s: %s", server.name, result)


def clear() -> None:
    """Test-only escape hatch.  Production code must never call this."""
    _CACHE.clear()
