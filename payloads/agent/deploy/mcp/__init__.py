"""Agent-payload MCP capability (issue #338).

Owns the deploy-time MCP surface for the agent payload: the ``McpStep``
provision step plus the MCP server validation / registration logic it
drives (:mod:`payloads.agent.deploy.mcp.validate`,
:mod:`payloads.agent.deploy.mcp.deploy`) and the deploy-time adapter
:class:`payloads.agent.deploy.mcp.brick.MCPBrick`.
"""

from __future__ import annotations

from payloads.agent.deploy.mcp.step import McpStep

__all__ = ["McpStep"]
