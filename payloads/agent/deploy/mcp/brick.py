"""Agent-payload MCP deploy adapter (epic #106; #107-#111; relocated by #338).

Registers MCP servers declared under
``instances/<app>/payloads/agent/mcp_servers/<name>/server.yml``. Each sub-directory is
its own server; the adapter deploys each one to a per-target state file
in the workspace via :func:`agent_foundry.state.save_state` (the
workspace is the single source of truth -- no local ``_state/`` cache,
see issue #50).

This is the canonical home for the MCP deploy logic (issue #338): MCP is
an agent-payload capability run by ``AgentPayloadAdapter.provision`` via
the payload's :data:`~payloads.agent.deploy.assembly.PROVISION_STEPS`
manifest.  :class:`~payloads.agent.deploy.mcp.step.McpStep` drives this
``MCPBrick`` to register servers and publish the ``mcp__<sub>`` -> URL
variables the App's ``app.yaml`` render substitutes.  The ``brick mcp``
CLI verbs (``deploy`` / ``validate`` / ``inspect`` / ``resolve``) are
registered in :mod:`agent_foundry.deploy._brick_registry`.

The provision step runs after ``skills`` and **before** the App deploy
so ``mcp__<sub>`` outputs are available for variable substitution during
App config rendering.

The agent runtime consumes these state files via
``payloads/agent/runtime/brick_mcp_servers.py:build_mcp_servers`` (PR 2 / #108), and
fetches server-supplied ``prompts`` / ``resources`` per request via
``payloads/agent/runtime/mcp_consumption.py`` (PR 4 / #110).  PR 5 (#111)
retired the legacy ``SKILL_TOOLS``-derived MCP-URL fallback so this is
the only MCP attachment path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Union

from pydantic import BaseModel, ConfigDict

from agent_foundry._instance_layout import payload_root, resolve_payload_subdir
from agent_foundry.bricks import (
    Brick,
    BrickResult,
    DeployContext,
    DestroyContext,
    DestroyPlan,
    DestroyPlanItem,
    StandardLayoutBrick,
)
from agent_foundry.destroy_base import delete_workspace_state, iter_nested_sub_instances, load_nested_state

# ---------------------------------------------------------------------------
# State schema (#176)
# ---------------------------------------------------------------------------
#
# Loose mirror of what ``payloads/agent/deploy/mcp/deploy.py:_build_state``
# persists, NOT a re-import of the deploy-time models in
# ``agent_foundry.contracts.mcp``.  Two reasons:
#
# 1. The on-disk shape is allowed to evolve independently of the
#    user-input YAML contract.  Reusing ``ServerConfig`` would mean a
#    tightening of any deploy-time validator immediately invalidates
#    every previously-persisted state file -- silently, on the next
#    ``deploy`` run.
# 2. Downstream consumers (the agent runtime in
#    ``payloads/agent/runtime/mcp_consumption.py``) only depend on a
#    handful of keys per server.  This schema captures exactly those.
#
# All submodels use ``extra="ignore"`` so the workspace-backed
# ``save_state`` ``_audit`` trail and any deploy-time-only fields are
# tolerated without churn here.


class McpToolsState(BaseModel):
    """``state["tools"]`` -- dump of the deploy-time ``ToolsConfig``.

    ``allow`` accepts both bare strings (exact upstream name) and
    dicts (the dumped form of :class:`ToolAllowEntry` with
    ``match`` / ``expose_as``).  Bare-string entries are the historical
    shape; dict entries land via ``expose_as`` aliases used by managed
    Genie / Functions / Vector Search servers.  Validating either
    shape here would re-implement the deploy-time validator -- we
    deliberately don't, leaving content checks to deploy time.
    """

    model_config = ConfigDict(extra="ignore")

    allow: list[Union[str, dict[str, Any]]] = []
    name_prefix: str | None = None


class McpPromptsState(BaseModel):
    """``state["prompts"]`` -- dump of the deploy-time ``PromptsConfig``."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool


class McpResourcesState(BaseModel):
    """``state["resources"]`` -- dump of the deploy-time ``ResourcesConfig``."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool


class McpServerState(BaseModel):
    """Contract for ``<workspace>/.../mcp_servers/<sub>/<target>.json`` (#176).

    Built by ``payloads/agent/deploy/mcp/deploy.py:_build_state`` and persisted
    by the workspace-backed :func:`agent_foundry.state.save_state` --
    each per-sub-instance file therefore also carries an ``_audit``
    trail, tolerated here via ``extra="ignore"``.

    ``auto_poll`` is genuinely optional: ``_build_state`` only writes
    the key when the deploy-time config resolved to a non-None
    ``AutoPollConfig`` (deploy.py:126-127).  Absence means "off"; we do
    NOT persist ``null`` to keep the runtime free of an extra
    absent-vs-null branch.  Type is ``dict | None`` rather than a
    declared ``McpAutoPollState`` -- the runtime poller in
    ``payloads/agent/runtime/mcp_wrapper.py`` reads keys defensively,
    and re-declaring the full ``AutoPollConfig`` shape here would
    duplicate the deploy-time validator without adding value.
    """

    model_config = ConfigDict(extra="ignore")

    url: str
    type: str
    name_prefix: str
    tools: McpToolsState
    prompts: McpPromptsState
    resources: McpResourcesState
    auto_poll: dict[str, Any] | None = None


class MCPBrick(StandardLayoutBrick):
    """Deploy MCP servers declared under ``<instance>/mcp_servers/``.

    Each direct subdirectory (not starting with ``_``) is treated as a
    sub-instance whose ``server.yml`` describes one MCP server. The
    deploy step writes per-target state to the workspace at
    ``/Shared/agent-foundry/<instance>/_state/mcp_servers/<sub>/<target>.json``
    via :func:`agent_foundry.state.save_state`.  No local ``_state/``
    file is created -- the workspace is the single source of truth
    (issue #50).

    State outputs follow the nested-layout convention: each sub-instance
    publishes ``mcp__<sub_instance>`` -> resolved URL, symmetric with
    ``genie_space_id__<sub>`` and ``vs_index_name__<sub>``.
    """

    name = "mcp"
    display_name = "MCP Servers"
    # Slot 75 sits between ``skills`` (70) and ``agent_app`` (90).
    # Spacing of 5 leaves room for third-party bricks to insert without
    # forcing a renumber of either neighbour.
    deploy_order = 75
    state_schema: type[BaseModel] | None = McpServerState

    # Nested layout: workspace state at
    # /Shared/agent-foundry/<instance>/_state/mcp_servers/<name>/<target>.json.
    # No local _state/ directory is created (issue #50).
    state_subdir = "mcp_servers"
    state_keys = {"url": "mcp"}
    is_nested_layout = True

    def _state_parent(self, ctx: DeployContext) -> Path:
        # MCP authoring + state live under the agent payload
        # (instances/<inst>/payloads/agent/mcp_servers/), not the
        # instance root -- keep the read in sync with the deploy write.
        return resolve_payload_subdir(ctx.instance_dir, self.state_subdir)

    def applies_to(self, ctx: DeployContext) -> bool:
        d = resolve_payload_subdir(ctx.instance_dir, "mcp_servers")
        if not d.is_dir():
            return False
        return any(
            child.is_dir() and not child.name.startswith(("_", ".")) and (child / "server.yml").exists()
            for child in d.iterdir()
        )

    def deploy(self, ctx: DeployContext) -> list[BrickResult]:
        # Imports kept inside ``deploy`` so the brick module stays
        # cheap to import (registry traversal happens on every CLI
        # invocation; deploy scripts are heavier).
        from payloads.agent.deploy.mcp.deploy import deploy as deploy_server

        mcp_dir = resolve_payload_subdir(ctx.instance_dir, "mcp_servers")
        results: list[BrickResult] = []
        for sub in sorted(d for d in mcp_dir.iterdir() if d.is_dir() and not d.name.startswith(("_", "."))):
            if not (sub / "server.yml").exists():
                continue

            print(f"\n  [{sub.name}] Deploying MCP server...")
            rc = deploy_server(sub, ctx.target, ctx.variables, dry_run=ctx.dry_run, ws=ctx.ws)
            results.append(
                BrickResult(
                    name=sub.name,
                    status="ok" if rc == 0 else "deploy_error",
                    rc=rc,
                )
            )

        # Re-publish state so downstream bricks in this run see the
        # fresh ``mcp__<sub>`` variables (matches the genie / vector_search
        # pattern).
        ctx.variables.update(self.load_state_outputs(ctx))
        return results

    def plan_destroy(self, ctx: DestroyContext) -> DestroyPlan:
        items: list[DestroyPlanItem] = []
        for sub in iter_nested_sub_instances(payload_root(ctx.instance_dir), self.state_subdir):
            state = load_nested_state(
                ctx,
                brick_type="mcp_servers",
                sub_instance=sub.name,
                schema=self.state_schema,
                brick_name=self.name,
            )
            if not state.get("url"):
                continue
            items.append(
                DestroyPlanItem(
                    brick=self.name,
                    sub_instance=sub.name,
                    resource=f"MCP server state {sub.name}",
                    destructive=False,
                )
            )
        return DestroyPlan(items=items)

    def destroy(self, ctx: DestroyContext) -> list[BrickResult]:
        results: list[BrickResult] = []
        for sub in iter_nested_sub_instances(payload_root(ctx.instance_dir), self.state_subdir):
            state = load_nested_state(
                ctx,
                brick_type="mcp_servers",
                sub_instance=sub.name,
                schema=self.state_schema,
                brick_name=self.name,
            )
            if not state.get("url"):
                results.append(
                    BrickResult(
                        name=sub.name,
                        status="skipped",
                        rc=0,
                        details={"reason": "no state"},
                    )
                )
                continue
            if ctx.dry_run:
                print(f"  [DRY RUN] Would delete MCP state for {sub.name}")
                results.append(BrickResult(name=sub.name, status="ok", rc=0))
                continue
            delete_workspace_state(
                ctx,
                brick_type="mcp_servers",
                sub_instance=sub.name,
            )
            print(f"  Deleted MCP state for {sub.name}")
            results.append(BrickResult(name=sub.name, status="ok", rc=0))
        return results


# Runtime check: the adapter must satisfy the Brick protocol.
_: Brick = MCPBrick()
