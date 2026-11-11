"""Genie Spaces brick adapter (issue #84, 68-C)."""

from __future__ import annotations

import json

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


class GenieSpaceState(BaseModel):
    """Contract for the ``genie_spaces`` workspace state of one sub-instance (#173).

    Written by ``bricks/genie/scripts/deploy.py`` via the workspace-
    backed ``save_state`` helper, so each per-sub-instance file also
    carries an ``_audit`` trail appended by
    :func:`agent_foundry.state._append_audit`.  ``extra="ignore"``
    tolerates that audit trail (and any future-version fields) without
    failing validation.
    """

    model_config = ConfigDict(extra="ignore")

    space_id: str
    title: str


class GenieSpacesBrick(StandardLayoutBrick):
    name = "genie_spaces"
    display_name = "Genie Spaces"
    deploy_order = 50
    state_schema: type[BaseModel] | None = GenieSpaceState

    # Nested layout: one workspace state per <instance>/genie_spaces/<space>.
    state_subdir = "genie_spaces"
    state_keys = {"space_id": "genie_space_id"}
    is_nested_layout = True

    def applies_to(self, ctx: DeployContext) -> bool:
        d = ctx.instance_dir / "genie_spaces"
        if not d.is_dir():
            return False
        return any(
            child.is_dir() and not child.name.startswith("_") and (child / "space.yml").exists()
            for child in d.iterdir()
        )

    def deploy(self, ctx: DeployContext) -> list[BrickResult]:
        from bricks.genie.scripts.assemble import assemble_api_payload
        from bricks.genie.scripts.deploy import deploy as deploy_space
        from bricks.genie.scripts.validate import validate_serialized_space

        genie_dir = ctx.instance_dir / "genie_spaces"
        results: list[BrickResult] = []
        for sub in sorted(d for d in genie_dir.iterdir() if d.is_dir() and not d.name.startswith("_")):
            if not (sub / "space.yml").exists():
                continue

            print(f"\n  [{sub.name}] Assembling...")
            try:
                payload = assemble_api_payload(sub, ctx.variables)
            except Exception as exc:
                print(f"  [{sub.name}] ASSEMBLE FAILED: {exc}")
                results.append(
                    BrickResult(
                        name=sub.name,
                        status="assemble_error",
                        rc=1,
                        details={"error": str(exc)},
                    )
                )
                continue

            ss = json.loads(payload["serialized_space"])
            errors = validate_serialized_space(ss)
            if errors:
                print(f"  [{sub.name}] VALIDATION FAILED:")
                for e in errors:
                    print(f"    - {e}")
                results.append(
                    BrickResult(
                        name=sub.name,
                        status="validate_error",
                        rc=1,
                        details={"errors": errors},
                    )
                )
                continue

            n_tables = len(ss.get("data_sources", {}).get("tables", []))
            print(f"  [{sub.name}] Validated OK ({n_tables} tables)")

            rc = deploy_space(sub, ctx.target, ctx.variables, action=None, dry_run=ctx.dry_run, ws=ctx.ws)
            results.append(
                BrickResult(
                    name=sub.name,
                    status="ok" if rc == 0 else "deploy_error",
                    rc=rc,
                )
            )
            if ctx.dry_run and rc == 0:
                ctx.variables.setdefault(f"genie_space_id__{sub.name}", "<DRY_RUN>")

        # Re-publish state so downstream bricks in this run see the
        # fresh genie_space_id__<sub> variables.
        if not ctx.dry_run:
            ctx.variables.update(self.load_state_outputs(ctx))
        return results

    def plan_destroy(self, ctx: DestroyContext) -> DestroyPlan:
        items: list[DestroyPlanItem] = []
        for sub in iter_nested_sub_instances(ctx.instance_dir, self.state_subdir):
            state = load_nested_state(
                ctx,
                brick_type="genie_spaces",
                sub_instance=sub.name,
                schema=self.state_schema,
                brick_name=self.name,
            )
            space_id = state.get("space_id")
            if not space_id:
                continue
            title = state.get("title", sub.name)
            items.append(
                DestroyPlanItem(
                    brick=self.name,
                    sub_instance=sub.name,
                    resource=f"Genie space {title} ({space_id})",
                    destructive=False,
                )
            )
        return DestroyPlan(items=items)

    def destroy(self, ctx: DestroyContext) -> list[BrickResult]:
        from bricks.genie.scripts.deploy import deploy as deploy_space

        results: list[BrickResult] = []
        for sub in iter_nested_sub_instances(ctx.instance_dir, self.state_subdir):
            state = load_nested_state(
                ctx,
                brick_type="genie_spaces",
                sub_instance=sub.name,
                schema=self.state_schema,
                brick_name=self.name,
            )
            if not state.get("space_id"):
                results.append(nothing_to_destroy(sub.name))
                continue
            rc = deploy_space(sub, ctx.target, ctx.variables, action="destroy", dry_run=ctx.dry_run, ws=ctx.ws)
            results.append(
                BrickResult(
                    name=sub.name,
                    status="ok" if rc == 0 else "destroy_error",
                    rc=rc,
                    deleted=deleted_refs(ctx, rc, "genie_space", state["space_id"]),
                )
            )
        return results


_: Brick = GenieSpacesBrick()
