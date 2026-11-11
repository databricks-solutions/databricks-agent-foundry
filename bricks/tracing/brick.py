"""MLflow Experiment brick adapter (issue #84, 68-C; split per #151).

The ``tracing`` brick here deploys just the UC-backed MLflow experiment
used for tracing (renamed from the misnamed ``agent`` brick in #338).
Runtime agent code lives under :mod:`payloads.agent.runtime` and is not
part of the deploy orchestration -- 68-D enforces that the runtime side
never imports this module.
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


class MlflowExperimentState(BaseModel):
    """Contract for the ``tracing`` brick's persisted state (#172, #338).

    Written by ``bricks/tracing/scripts/ensure_experiment.py`` via the
    workspace-backed :func:`agent_foundry.state.save_state` (the single
    source of truth, shared across users and CI), so every persisted
    file also carries an ``_audit`` trail -- tolerated here via
    ``extra="ignore"``.
    """

    model_config = ConfigDict(extra="ignore")

    experiment_id: str


class MlflowExperimentBrick(BrickDestroyDefaults):
    name = "tracing"
    display_name = "MLflow Experiment"
    deploy_order = 40
    state_schema: type[BaseModel] | None = MlflowExperimentState

    def applies_to(self, ctx: DeployContext) -> bool:
        from agent_foundry.payload_requirements import payload_requires_brick

        return payload_requires_brick(ctx.instance_dir, self.name)

    def deploy(self, ctx: DeployContext) -> list[BrickResult]:
        from bricks.tracing.scripts.ensure_experiment import ensure_experiment

        print(f"\n  [{ctx.instance_dir.name}] Ensuring MLflow experiment...")
        rc = ensure_experiment(ctx.instance_dir, ctx.target, ctx.variables, dry_run=ctx.dry_run, ws=ctx.ws)
        return [
            BrickResult(
                name=ctx.instance_dir.name,
                status="ok" if rc == 0 else "deploy_error",
                rc=rc,
            )
        ]

    def load_state_outputs(self, ctx: DeployContext) -> dict[str, str]:
        """Publish ``experiment_id`` from the workspace-backed state store.

        State lives only in the workspace (``brick_type="tracing"`` +
        ``sub_instance=instance_name``), not under a local
        ``<instance>/agent/_state`` tree (#338), so -- like
        :class:`ObservabilityBrick` -- the brick reads it directly and
        validates against :class:`MlflowExperimentState`.
        """
        from agent_foundry.state import load_state, validate_state

        instance_name = ctx.instance_dir.name
        state = load_state(ctx.ws, instance_name, "tracing", instance_name, ctx.target)
        if not state:
            return {}
        validate_state(self.name, state, self.state_schema)
        experiment_id = state.get("experiment_id")
        return {"experiment_id": str(experiment_id)} if experiment_id else {}

    def plan_destroy(self, ctx: DestroyContext) -> DestroyPlan:
        from agent_foundry.state import load_state

        instance_name = ctx.instance_dir.name
        state = load_state(ctx.ws, instance_name, "tracing", instance_name, ctx.target)
        experiment_id = state.get("experiment_id") or ctx.variables.get("experiment_id")
        if not experiment_id:
            return DestroyPlan()
        items = [
            DestroyPlanItem(
                brick=self.name,
                resource=f"MLflow experiment {experiment_id}",
                destructive=False,
            ),
            DestroyPlanItem(
                brick=self.name,
                sub_instance="trace_tables",
                resource="UC trace tables (from experiment tags)",
                destructive=True,
                reason="drops Delta trace tables created by the experiment",
            ),
            DestroyPlanItem(
                brick=self.name,
                sub_instance="trace_schema",
                resource="UC trace schema (if empty after table drop)",
                destructive=True,
                reason="drops the trace schema this brick created (RESTRICT -- preserves user tables)",
            ),
        ]
        return DestroyPlan(items=items)

    def destroy(self, ctx: DestroyContext) -> list[BrickResult]:
        from bricks.tracing.scripts.ensure_experiment import destroy_experiment, resolve_experiment_id

        experiment_id = resolve_experiment_id(
            ctx.instance_dir, ctx.target, ctx.variables, ws=ctx.ws, dry_run=ctx.dry_run
        )
        if not experiment_id:
            return [nothing_to_destroy(ctx.instance_dir.name)]
        rc = destroy_experiment(
            ctx.instance_dir,
            ctx.target,
            ctx.variables,
            ws=ctx.ws,
            dry_run=ctx.dry_run,
            drop_trace_tables=ctx.force,
            experiment_id=experiment_id,
        )
        return [
            BrickResult(
                name=ctx.instance_dir.name,
                status="ok" if rc == 0 else "destroy_error",
                rc=rc,
                deleted=deleted_refs(ctx, rc, "mlflow_experiment", experiment_id),
            )
        ]


_: Brick = MlflowExperimentBrick()
