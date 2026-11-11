"""MCP provisioning step for the agent payload (issue #338).

Folds the former standalone ``MCPBrick`` (deploy_order 75) into the
agent payload's provision manifest: validate + register the instance's
MCP servers, persist per-server workspace state, and publish the
``mcp__<sub>`` -> URL variables that the App's ``app.yaml`` render
substitutes.  The step runs after skills and **before** the App deploy
so those variables land in ``ctx.variables`` before the resource
templates resolve -- the central ordering guarantee of this slice.

Runtime consumption is unchanged -- the agent runtime reads the
workspace MCP state directly, never this step.  The deploy-time
implementation (validate / register / ``McpServerState`` schema) lives
alongside this step under :mod:`payloads.agent.deploy.mcp` and is driven
through the :class:`~payloads.agent.deploy.mcp.brick.MCPBrick` adapter.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_foundry.bricks import (
        BrickResult,
        DeployContext,
        DestroyContext,
        DestroyPlanItem,
    )
    from payloads.agent.deploy.mcp.brick import MCPBrick


class McpStep:
    """Deploy + tear down the instance's MCP servers.

    Thin manifest wrapper over the deploy-time ``MCPBrick`` logic, which
    is not in the brick registry (issue #338); this step drives it from
    the payload's provision manifest.  A fresh brick instance is built per
    call -- it is stateless -- so the assembly module stays cheap to
    import (no SDK / brick import at manifest-construction time).
    """

    name = "mcp"

    def _brick(self) -> MCPBrick:
        from payloads.agent.deploy.mcp.brick import MCPBrick

        return MCPBrick()

    def provision(self, ctx: DeployContext) -> int:
        """Register every MCP server; publish ``mcp__<sub>`` into ``ctx.variables``.

        Self-gates (returns 0) when the instance has no ``mcp_servers/``
        tree.  ``MCPBrick.deploy`` republishes the resolved server URLs
        into ``ctx.variables`` in place before this returns, so the App
        deploy that follows resolves ``${var.mcp__<sub>}`` templates.
        Returns the first non-zero per-server rc so a failed server
        blocks the App deploy (fail-fast manifest).
        """
        brick = self._brick()
        if not brick.applies_to(ctx):
            return 0
        results = brick.deploy(ctx)
        return next((r.rc for r in results if r.rc != 0), 0)

    def plan_destroy(self, ctx: DestroyContext) -> list[DestroyPlanItem]:
        """Plan removal of each server's workspace state (non-destructive)."""
        return list(self._brick().plan_destroy(ctx).items)

    def destroy(self, ctx: DestroyContext) -> list[BrickResult]:
        """Delete each server's workspace MCP state."""
        return self._brick().destroy(ctx)
