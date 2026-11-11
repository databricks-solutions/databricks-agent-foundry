"""Observability brick adapter (issue #84, 68-C).

The observability brick is last in the deploy order because the
dashboard references the MLflow experiment + trace tables that earlier
bricks set up.  Failures are reported as ``skipped`` (not
``deploy_error``) so a first-time deploy does not fail just because no
agent has run yet.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

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


class DashboardState(BaseModel):
    """Contract for the observability brick's persisted state (#177).

    Written by ``bricks/observability/scripts/deploy_dashboard.py:326-336``
    via the workspace-backed :func:`agent_foundry.state.save_state`, so
    every persisted file also carries an ``_audit`` trail (tolerated
    here via ``extra="ignore"``).

    No fields are intentionally optional: ``_save`` always writes all
    three together for both create and update paths.
    """

    model_config = ConfigDict(extra="ignore")

    dashboard_id: str
    etag: str
    display_name: str


class ObservabilityBrick(BrickDestroyDefaults):
    name = "observability"
    display_name = "Observability"
    deploy_order = 100
    state_schema: type[BaseModel] | None = DashboardState

    def applies_to(self, ctx: DeployContext) -> bool:
        from agent_foundry.payload_requirements import payload_requires_brick

        return payload_requires_brick(ctx.instance_dir, self.name)

    def deploy(self, ctx: DeployContext) -> list[BrickResult]:
        from bricks.observability.scripts.deploy_dashboard import deploy as deploy_dash

        print(f"\n  [{ctx.instance_dir.name}] Deploying observability dashboard...")
        rc = deploy_dash(ctx.instance_dir, ctx.target, ctx.variables, dry_run=ctx.dry_run, ws=ctx.ws)
        if rc != 0 and not ctx.dry_run:
            print(f"  [{ctx.instance_dir.name}] Observability skipped (experiment may not exist yet)")
            return [BrickResult(name=ctx.instance_dir.name, status="skipped", rc=rc)]
        return [
            BrickResult(
                name=ctx.instance_dir.name,
                status="ok" if rc == 0 else "skipped",
                rc=rc,
            )
        ]

    def load_state_outputs(self, ctx: DeployContext) -> dict[str, str]:
        """Validate the persisted dashboard state, then publish nothing.

        The brick does not subclass :class:`StandardLayoutBrick` (its
        state lives ONLY in the workspace at the unusual coordinates
        ``brick_type="dashboard"`` + ``sub_instance=instance_name``).
        That means the validation hook added by #156 to
        ``StandardLayoutBrick.load_state_outputs`` does NOT fire
        automatically -- this method has to call ``validate_state``
        directly.

        We deliberately publish no outputs (#157 audit finding):
        ``dashboard_id`` / ``etag`` / ``display_name`` have no actual
        downstream consumer in the framework today.  The evaluation
        brick consumes ``tracing_experiment`` from the *agent* brick
        (#172), not from observability.  Declaring outputs here would
        be cargo-culting; if a real consumer materialises (e.g. an
        agent_app surface that links to the dashboard), this method
        is the place to wire it.
        """
        from agent_foundry.state import load_state, validate_state

        instance_name = ctx.instance_dir.name
        state = load_state(
            ctx.ws,
            instance_name,
            "dashboard",
            instance_name,
            ctx.target,
        )
        if state:
            validate_state(self.name, state, self.state_schema)
        return {}

    def plan_destroy(self, ctx: DestroyContext) -> DestroyPlan:
        from agent_foundry.state import load_state

        instance_name = ctx.instance_dir.name
        state = load_state(
            ctx.ws,
            instance_name,
            "dashboard",
            instance_name,
            ctx.target,
        )
        dashboard_id = state.get("dashboard_id")
        if not dashboard_id:
            return DestroyPlan()
        display = state.get("display_name", dashboard_id)
        return DestroyPlan(
            items=[
                DestroyPlanItem(
                    brick=self.name,
                    resource=f"Lakeview dashboard {display} ({dashboard_id})",
                    destructive=False,
                )
            ]
        )

    def destroy(self, ctx: DestroyContext) -> list[BrickResult]:
        from agent_foundry.state import load_state
        from bricks.observability.scripts.deploy_dashboard import deploy as deploy_dash

        instance_name = ctx.instance_dir.name
        state = load_state(
            ctx.ws,
            instance_name,
            "dashboard",
            instance_name,
            ctx.target,
        )
        if not state.get("dashboard_id"):
            return [nothing_to_destroy(instance_name)]
        rc = deploy_dash(
            ctx.instance_dir,
            ctx.target,
            ctx.variables,
            action="destroy",
            dry_run=ctx.dry_run,
            ws=ctx.ws,
        )
        return [
            BrickResult(
                name=ctx.instance_dir.name,
                status="ok" if rc == 0 else "destroy_error",
                rc=rc,
                deleted=deleted_refs(ctx, rc, "dashboard", state.get("dashboard_id")),
            )
        ]


_: Brick = ObservabilityBrick()
