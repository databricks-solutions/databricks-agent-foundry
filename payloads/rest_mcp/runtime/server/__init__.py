"""The ``rest_mcp`` MCP server layer.

Turns a validated :class:`~payloads.rest_mcp.contracts.ApiSpec` into a running
MCP surface.  :mod:`payloads.rest_mcp.runtime.server.registry` is the thin,
FastMCP-free core: it wires the pure engine (declarative tools) and the
discovered code tools (Tier 3) onto a low-level MCP ``Server`` with dynamic,
config-derived ``inputSchema``s.  A later slice adds the ASGI host that mounts
one such server per API over Streamable HTTP.
"""

from __future__ import annotations
