"""Vector Search brick adapter (issue #84, 68-C)."""

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


class VectorIndexState(BaseModel):
    """Contract for the ``vector_indexes`` workspace state of one sub-instance (#174).

    Written by ``bricks/vector_search/scripts/deploy_index.py`` via the
    workspace-backed ``save_state`` helper: every create / adopt path
    produces all three fields together (see ``_create_index`` and the
    stale-state adoption branch), plus an ``_audit`` trail.  Destroy
    deletes the state.

    ``index_type`` is persisted but intentionally NOT exposed via
    ``state_keys`` (no downstream brick consumes it as a variable);
    declaring it here keeps the schema honest about the on-disk shape.
    ``extra="ignore"`` covers future-version metadata.
    """

    model_config = ConfigDict(extra="ignore")

    index_name: str
    endpoint_name: str
    index_type: str


class VectorIndexesBrick(StandardLayoutBrick):
    name = "vector_indexes"
    display_name = "Vector Search"
    deploy_order = 60
    state_schema: type[BaseModel] | None = VectorIndexState

    # Nested layout: each sub-instance is its own index.
    state_subdir = "vector_indexes"
    state_keys = {
        "index_name": "vs_index_name",
        "endpoint_name": "vs_endpoint_name",
    }
    is_nested_layout = True

    def applies_to(self, ctx: DeployContext) -> bool:
        d = ctx.instance_dir / "vector_indexes"
        if not d.is_dir():
            return False
        return any(
            child.is_dir() and not child.name.startswith(("_", ".")) and (child / "index_config.yml").exists()
            for child in d.iterdir()
        )

    def deploy(self, ctx: DeployContext) -> list[BrickResult]:
        from agent_foundry.loader import FoundryConfigError
        from bricks.vector_search.scripts.deploy_index import deploy as deploy_vs_index
        from bricks.vector_search.scripts.validate_index import (
            load_config,
            substitute_variables,
            validate_index_config,
        )

        vs_dir = ctx.instance_dir / "vector_indexes"
        results: list[BrickResult] = []
        for sub in sorted(d for d in vs_dir.iterdir() if d.is_dir() and not d.name.startswith(("_", "."))):
            if not (sub / "index_config.yml").exists():
                continue

            print(f"\n  [{sub.name}] Validating...")
            config = load_config(sub / "index_config.yml")
            config = substitute_variables(config, ctx.variables)
            try:
                validate_index_config(config, source=sub / "index_config.yml")
            except FoundryConfigError as exc:
                print(f"  [{sub.name}] VALIDATION FAILED:")
                print(f"    - {exc}")
                results.append(
                    BrickResult(
                        name=sub.name,
                        status="validate_error",
                        rc=1,
                        details={"errors": [str(exc)]},
                    )
                )
                continue

            idx_type = config.get("index", {}).get("index_type", "?")
            print(f"  [{sub.name}] Validated OK (type={idx_type})")

            rc = deploy_vs_index(sub, ctx.target, ctx.variables, action=None, dry_run=ctx.dry_run, ws=ctx.ws)
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
                brick_type="vector_indexes",
                sub_instance=sub.name,
                schema=self.state_schema,
                brick_name=self.name,
            )
            index_name = state.get("index_name")
            if not index_name:
                continue
            items.append(
                DestroyPlanItem(
                    brick=self.name,
                    sub_instance=sub.name,
                    resource=f"Vector Search index {index_name}",
                    destructive=False,
                )
            )
        return DestroyPlan(items=items)

    def destroy(self, ctx: DestroyContext) -> list[BrickResult]:
        from bricks.vector_search.scripts.deploy_index import deploy as deploy_vs_index

        results: list[BrickResult] = []
        for sub in iter_nested_sub_instances(ctx.instance_dir, self.state_subdir):
            state = load_nested_state(
                ctx,
                brick_type="vector_indexes",
                sub_instance=sub.name,
                schema=self.state_schema,
                brick_name=self.name,
            )
            if not state.get("index_name"):
                results.append(nothing_to_destroy(sub.name))
                continue
            rc = deploy_vs_index(sub, ctx.target, ctx.variables, action="destroy", dry_run=ctx.dry_run, ws=ctx.ws)
            results.append(
                BrickResult(
                    name=sub.name,
                    status="ok" if rc == 0 else "destroy_error",
                    rc=rc,
                    deleted=deleted_refs(ctx, rc, "vector_index", state.get("index_name")),
                )
            )
        return results


_: Brick = VectorIndexesBrick()
