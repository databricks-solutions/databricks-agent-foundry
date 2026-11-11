"""Deploy-time half of the ``rest_mcp`` payload (issue #326).

Mirrors ``payloads/agent/deploy``: the pieces the deploy adapter
(:mod:`payloads.rest_mcp.adapter`) composes to translate the payload's
config into the framework's neutral specs and to provision the payload-owned
Unity Catalog surface.  This slice ships the runtime-spec builder
(:mod:`payloads.rest_mcp.deploy.spec`); the UC connection / MCP-Service
``provision()`` step lands in a follow-on slice.
"""

from __future__ import annotations
