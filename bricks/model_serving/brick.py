"""Serverless Model Serving brick adapter."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from agent_foundry.bricks import (
    Brick,
    BrickResult,
    DeployContext,
    DestroyContext,
    DestroyPlan,
    DestroyPlanItem,
    ResourceRef,
    StandardLayoutBrick,
    deleted_refs,
)
from agent_foundry.destroy_base import iter_nested_sub_instances, load_nested_state, nothing_to_destroy
from agent_foundry.loader import FoundryConfigError

CONFIG_FILENAME = "endpoint_config.yml"


class ServingEndpointState(BaseModel):
    """Persisted state for one ``model_serving/<name>`` sub-instance."""

    model_config = ConfigDict(extra="ignore")

    endpoint_name: str
    model_name: str
    model_version: str
    build_hash: str = ""
    experiment_id: str = ""


def _deleted(ctx: DestroyContext, rc: int, state: dict[str, Any]) -> list[ResourceRef]:
    refs = deleted_refs(ctx, rc, "serving_endpoint", str(state["endpoint_name"]))
    if state.get("build_hash"):
        refs += deleted_refs(ctx, rc, "registered_model", str(state.get("model_name", "")))
        refs += deleted_refs(ctx, rc, "mlflow_experiment", str(state.get("experiment_id", "")))
    return refs


class ModelServingBrick(StandardLayoutBrick):
    name = "model_serving"
    display_name = "Model Serving"
    deploy_order = 65
    state_schema: type[BaseModel] | None = ServingEndpointState

    state_subdir = "model_serving"
    state_keys = {"endpoint_name": "serving_endpoint_name"}
    is_nested_layout = True

    def _sub_dirs(self, instance_dir: Path) -> list[Path]:
        return [
            sub
            for sub in iter_nested_sub_instances(instance_dir, self.state_subdir)
            if (sub / CONFIG_FILENAME).exists()
        ]

    def applies_to(self, ctx: DeployContext) -> bool:
        return bool(self._sub_dirs(ctx.instance_dir))

    def deploy(self, ctx: DeployContext) -> list[BrickResult]:
        from bricks.model_serving.scripts.deploy_endpoint import deploy as deploy_endpoint

        results: list[BrickResult] = []
        for sub in self._sub_dirs(ctx.instance_dir):
            try:
                rc = deploy_endpoint(sub, ctx.target, ctx.variables, dry_run=ctx.dry_run, ws=ctx.ws)
            except FoundryConfigError as exc:
                results.append(
                    BrickResult(name=sub.name, status="validate_error", rc=1, details={"errors": [str(exc)]})
                )
                continue
            except (OSError, ValueError, RuntimeError) as exc:
                print(f"  [{sub.name}] ERROR: {exc}")
                results.append(BrickResult(name=sub.name, status="deploy_error", rc=1, details={"error": str(exc)}))
                continue
            results.append(BrickResult(name=sub.name, status="ok" if rc == 0 else "deploy_error", rc=rc))

        ctx.variables.update(self.load_state_outputs(ctx))
        return results

    def _state(self, ctx: DestroyContext, sub: Path) -> dict[str, Any]:
        return load_nested_state(
            ctx,
            brick_type=self.state_subdir,
            sub_instance=sub.name,
            schema=self.state_schema,
            brick_name=self.name,
        )

    def plan_destroy(self, ctx: DestroyContext) -> DestroyPlan:
        items: list[DestroyPlanItem] = []
        for sub in iter_nested_sub_instances(ctx.instance_dir, self.state_subdir):
            state = self._state(ctx, sub)
            if not state.get("endpoint_name"):
                continue
            built = " and its built model" if state.get("build_hash") else ""
            items.append(
                DestroyPlanItem(
                    brick=self.name,
                    sub_instance=sub.name,
                    resource=f"Serving endpoint {state['endpoint_name']}{built}",
                    destructive=True,
                    reason="deletes the endpoint; a built model is deleted with all its versions",
                )
            )
        return DestroyPlan(items=items)

    def destroy(self, ctx: DestroyContext) -> list[BrickResult]:
        from bricks.model_serving.scripts.deploy_endpoint import deploy as deploy_endpoint

        results: list[BrickResult] = []
        for sub in iter_nested_sub_instances(ctx.instance_dir, self.state_subdir):
            state = self._state(ctx, sub)
            if not state.get("endpoint_name"):
                results.append(nothing_to_destroy(sub.name))
                continue
            if not ctx.force:
                results.append(
                    BrickResult(name=sub.name, status="skipped", rc=0, details={"reason": "requires --force"})
                )
                continue
            rc = deploy_endpoint(sub, ctx.target, ctx.variables, action="destroy", dry_run=ctx.dry_run, ws=ctx.ws)
            results.append(
                BrickResult(
                    name=sub.name,
                    status="ok" if rc == 0 else "destroy_error",
                    rc=rc,
                    deleted=_deleted(ctx, rc, state),
                )
            )
        return results


_: Brick = ModelServingBrick()
