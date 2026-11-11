"""The ``rest_mcp`` App payload -- a config-driven REST->MCP proxy (issue #326).

A Databricks App payload that wraps one or more arbitrary REST APIs as a
governed MCP server: the config-driven REST->MCP translation layer the
platform does not ship, attached the native Databricks way so it inherits
AI Gateway / MCP Service governance.  Selected via ``payload.adapter = rest_mcp``.

The framework knows only the generic payload envelope
(:class:`agent_foundry.contracts.payload.PayloadFile` -- ``adapter`` + an
opaque ``spec``); this package defines what its ``spec`` means (see
:mod:`payloads.rest_mcp.contracts`) and, in later slices, the in-container
runtime engine and the deploy-time adapter.

The payload lands incrementally across stacked PRs: this slice introduces
the package and its typed ``spec`` contract; the pure REST->MCP engine, the
code-tool escape hatch, the FastMCP/FastAPI host, and the deploy adapter
follow.
"""

from __future__ import annotations
