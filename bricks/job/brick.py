"""Databricks Jobs brick adapter."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from agent_foundry.bricks import (
    Brick,
    BrickResult,
    DeployContext,
    DestroyContext,
    DestroyPlan,
    DestroyPlanItem,
    StandardLayoutBrick,
    deleted_refs,
)
from agent_foundry.destroy_base import iter_nested_sub_instances, load_nested_state, nothing_to_destroy
from agent_foundry.loader import FoundryConfigError


class JobState(BaseModel):
    """Persisted state for one ``jobs/<name>`` sub-instance."""

    model_config = ConfigDict(extra="ignore")

    job_id: str
    job_name: str


class JobsBrick(StandardLayoutBrick):
    name = "jobs"
    display_name = "Jobs"
    deploy_order = 70
    state_schema: type[BaseModel] | None = JobState

    state_subdir = "jobs"
    state_keys = {"job_id": "job_id"}
    is_nested_layout = True

    def applies_to(self, ctx: DeployContext) -> bool:
        jobs_dir = ctx.instance_dir / self.state_subdir
        if not jobs_dir.is_dir():
            return False
        return any(
            child.is_dir() and not child.name.startswith(("_", ".")) and (child / "job.yml").exists()
            for child in jobs_dir.iterdir()
        )

    def deploy(self, ctx: DeployContext) -> list[BrickResult]:
        from bricks.job.scripts.deploy_job import deploy as deploy_job
        from bricks.job.scripts.validate_job import load_job_config, only_vs_index_outputs_missing

        results: list[BrickResult] = []
        for sub in iter_nested_sub_instances(ctx.instance_dir, self.state_subdir):
            if not (sub / "job.yml").exists():
                continue
            try:
                load_job_config(sub, ctx.variables)
            except FoundryConfigError as exc:
                if only_vs_index_outputs_missing(exc):
                    print(
                        f"  [{sub.name}] Skipping job -- Vector Search index output is not in the variable bag ({exc})."
                    )
                    results.append(
                        BrickResult(
                            name=sub.name,
                            status="skipped",
                            rc=0,
                            details={"reason": str(exc)},
                        )
                    )
                    continue
                results.append(
                    BrickResult(
                        name=sub.name,
                        status="validate_error",
                        rc=1,
                        details={"errors": [str(exc)]},
                    )
                )
                continue
            except (OSError, ValueError) as exc:
                results.append(
                    BrickResult(
                        name=sub.name,
                        status="validate_error",
                        rc=1,
                        details={"errors": [str(exc)]},
                    )
                )
                continue

            rc = deploy_job(
                sub,
                ctx.target,
                ctx.variables,
                dry_run=ctx.dry_run,
                ws=ctx.ws,
            )
            results.append(
                BrickResult(
                    name=sub.name,
                    status="ok" if rc == 0 else "deploy_error",
                    rc=rc,
                )
            )

        ctx.variables.update(self.load_state_outputs(ctx))
        return results

    def plan_destroy(self, ctx: DestroyContext) -> DestroyPlan:
        items: list[DestroyPlanItem] = []
        for sub in iter_nested_sub_instances(ctx.instance_dir, self.state_subdir):
            state = load_nested_state(
                ctx,
                brick_type=self.state_subdir,
                sub_instance=sub.name,
                schema=self.state_schema,
                brick_name=self.name,
            )
            job_id = state.get("job_id")
            if job_id:
                items.append(
                    DestroyPlanItem(
                        brick=self.name,
                        sub_instance=sub.name,
                        resource=f"Databricks Job {state.get('job_name', sub.name)} ({job_id})",
                        destructive=True,
                        reason="deletes the job definition and its run history",
                    )
                )
        return DestroyPlan(items=items)

    def destroy(self, ctx: DestroyContext) -> list[BrickResult]:
        from bricks.job.scripts.deploy_job import deploy as deploy_job

        results: list[BrickResult] = []
        for sub in iter_nested_sub_instances(ctx.instance_dir, self.state_subdir):
            state = load_nested_state(
                ctx,
                brick_type=self.state_subdir,
                sub_instance=sub.name,
                schema=self.state_schema,
                brick_name=self.name,
            )
            if not state.get("job_id"):
                results.append(nothing_to_destroy(sub.name))
                continue
            if not ctx.force:
                results.append(
                    BrickResult(
                        name=sub.name,
                        status="skipped",
                        rc=0,
                        details={"reason": "requires --force"},
                    )
                )
                continue
            rc = deploy_job(
                sub,
                ctx.target,
                ctx.variables,
                action="destroy",
                dry_run=ctx.dry_run,
                ws=ctx.ws,
            )
            results.append(
                BrickResult(
                    name=sub.name,
                    status="ok" if rc == 0 else "destroy_error",
                    rc=rc,
                    deleted=deleted_refs(ctx, rc, "job", str(state["job_id"])),
                )
            )
        return results


_: Brick = JobsBrick()
