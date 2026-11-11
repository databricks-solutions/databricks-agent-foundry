"""Agent App brick adapter (issue #84, 68-C; #338).

The App brick is the deploy-time host for a Databricks App payload.  It
is payload-agnostic: it resolves the payload selected by
``payloads/<payload>/payload.yml`` (via the generic
:func:`~agent_foundry.deploy.payload_adapters.load_payload_config`) and
drives its deploy-time contract through the
:class:`~agent_foundry.deploy.payload_adapters.AppPayloadAdapter` seam --
``provision``, the App deploy itself, ``load_state_outputs``, and
``plan_destroy`` / ``destroy``.  All payload-specific knowledge (e.g. the
agent payload's chat-history gating and Lakebase state) lives in the
payload (:mod:`payloads.agent`), never here.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel

from agent_foundry._instance_layout import discover_payloads, has_payload
from agent_foundry.bricks import (
    Brick,
    BrickDestroyDefaults,
    BrickResult,
    DeployContext,
    DestroyContext,
    DestroyPlan,
    DestroyPlanItem,
    deleted_refs,
)
from agent_foundry.destroy_base import nothing_to_destroy

if TYPE_CHECKING:
    from agent_foundry.deploy.payload_adapters import AppPayloadAdapter


def _resolve_payload(instance_dir: Path) -> AppPayloadAdapter:
    """Resolve the App payload adapter selected by ``app/payload.yml``.

    Local imports keep the brick descriptor loadable at registry-scan
    time on machines without the SDK (only ``deploy`` / ``destroy``
    actually need the payload).
    """
    from agent_foundry.deploy.payload_adapters import get_payload_adapter, load_payload_config

    cfg = load_payload_config(instance_dir)
    return get_payload_adapter(cfg.adapter)


class AppBrick(BrickDestroyDefaults):
    name = "app"
    display_name = "Agent App"
    deploy_order = 90
    state_schema: type[BaseModel] | None = None

    def applies_to(self, ctx: DeployContext) -> bool:
        # The generic App host (issue #337) carries a payload selected by
        # ``payloads/<payload>/payload.yml``.  Deploy when the instance has
        # any payload manifest (e.g. ``rest_mcp``) or the legacy agent
        # payload surface (``payloads/agent/`` may predate a manifest).
        return bool(discover_payloads(ctx.instance_dir)) or has_payload(ctx.instance_dir)

    def deploy(self, ctx: DeployContext) -> list[BrickResult]:
        from bricks.app.scripts.deploy_agent_app import deploy as deploy_bundled_app

        app_resource = ctx.variables.get(
            "app_resource_name",
            ctx.variables.get("app_name", ctx.instance_dir.name),
        )
        payload = _resolve_payload(ctx.instance_dir)

        # Step 1: payload deploy-time provisioning (e.g. the agent
        # payload's Lakebase chat-history project).  Runs first so any
        # discovered outputs are in ``ctx.variables`` before the App
        # deploy resolves resource templates.  ``provision`` mutates
        # ``ctx.variables`` in place and gates itself (no-op when the
        # capability is disabled).
        rc = payload.provision(ctx)
        if rc != 0:
            return [BrickResult(name=app_resource, status="deploy_error", rc=rc)]

        # Step 2: App sync + create + deploy via the payload-driven host.
        print(f"\n  [{app_resource}] Deploying App ({payload.name} payload)...")
        rc = deploy_bundled_app(
            app_resource,
            ctx.target,
            ctx.variables,
            instance_dir=ctx.instance_dir,
            dry_run=ctx.dry_run,
            ws=ctx.ws,
        )
        return [
            BrickResult(
                name=app_resource,
                status="ok" if rc == 0 else "deploy_error",
                rc=rc,
            )
        ]

    def load_state_outputs(self, ctx: DeployContext) -> dict[str, str]:
        """Publish the App's state outputs as deploy variables.

        Two independent sources are merged:

        * the deployed App URL (``app_url``) -- workspace-backed ``app``
          state written by the App-host engine after deploy (issue #337);
          only resolvable when ``ctx.ws`` is available.  Engine-owned, so
          the host reads it directly.
        * payload-owned state -- delegated to the resolved payload adapter
          via :meth:`AppPayloadAdapter.load_state_outputs` so the host
          never learns a payload's internals (e.g. the agent payload's
          Lakebase chat-history paths).

        Returns an empty dict when neither source has state yet (first
        deploy, or the payload published nothing).
        """
        outputs: dict[str, str] = {}

        # App URL (workspace-backed; engine-owned). Local import keeps
        # the brick descriptor importable without the SDK at registry
        # scan time.
        from agent_foundry.deploy.app_host import load_app_state

        app_state = load_app_state(ctx.ws, ctx.instance_dir.name, ctx.target)
        if "app_url" in app_state:
            outputs["app_url"] = app_state["app_url"]

        # Payload-owned state (republished cached resource paths, etc.).
        outputs.update(_resolve_payload(ctx.instance_dir).load_state_outputs(ctx))

        return outputs

    def plan_destroy(self, ctx: DestroyContext) -> DestroyPlan:
        app_resource = ctx.variables.get(
            "app_resource_name",
            ctx.variables.get("app_name", ctx.instance_dir.name),
        )
        items = [
            DestroyPlanItem(
                brick=self.name,
                resource=f"Databricks App {app_resource}",
                destructive=False,
            )
        ]
        items.extend(_resolve_payload(ctx.instance_dir).plan_destroy(ctx))
        return DestroyPlan(items=items)

    def destroy(self, ctx: DestroyContext) -> list[BrickResult]:
        from agent_foundry.deploy.app_host import app_exists
        from bricks.app.scripts.deploy_agent_app import destroy as destroy_app

        app_resource = ctx.variables.get(
            "app_resource_name",
            ctx.variables.get("app_name", ctx.instance_dir.name),
        )
        results: list[BrickResult] = []

        app_existed = ctx.ws is None or app_exists(ctx.ws, app_resource)
        rc = destroy_app(
            app_resource,
            ctx.target,
            ctx.variables,
            instance_dir=ctx.instance_dir,
            dry_run=ctx.dry_run,
            ws=ctx.ws,
        )
        if rc == 0 and not app_existed:
            results.append(nothing_to_destroy(app_resource))
        else:
            results.append(
                BrickResult(
                    name=app_resource,
                    status="ok" if rc == 0 else "destroy_error",
                    rc=rc,
                    deleted=deleted_refs(ctx, rc, "app", app_resource),
                )
            )

        results.extend(_resolve_payload(ctx.instance_dir).destroy(ctx))
        return results


_: Brick = AppBrick()
