"""In-container runtime for the ``rest_mcp`` payload (issue #326).

Hosts the pure REST->MCP translation engine (:mod:`payloads.rest_mcp.runtime.engine`)
and, in later slices, the FastMCP/FastAPI server that mounts one MCP surface
per configured API.  The engine is a pure library -- it imports no FastAPI,
MCP, or Databricks SDK -- so the mapping logic is unit-testable in isolation
and the server layer stays thin.

This package is also the instance-facing contract for Tier-3 code tools: an
instance module imports :class:`HttpClient` and :func:`tool` from here (the
container exposes it as ``rest_mcp``) to author a bespoke tool bound by the
payload's rails.
"""

from __future__ import annotations

from payloads.rest_mcp.runtime.code_tools import CodeTool, discover_code_tools, tool
from payloads.rest_mcp.runtime.http_client import HttpClient

__all__ = ["CodeTool", "HttpClient", "discover_code_tools", "tool"]
