"""
SQL Function brick adapter (issue #82, 68-B)
=============================================
Implements :class:`agent_foundry.bricks.Brick` over the existing
``bricks/sql_function/scripts/deploy_functions.py`` script so the
orchestrator can dispatch via the registry.

``sql_function`` is the simplest built-in brick (no dependencies, pure
SDK wrapper), which is why 68-B migrates it first to prove the protocol
before the larger 68-C rollout. The deployed function names live in the
shared workspace state so destroy works from any checkout.
"""

from __future__ import annotations

from pydantic import BaseModel

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
from agent_foundry.destroy_base import iter_nested_sub_instances, nothing_to_destroy


class SqlFunctionBrick(BrickDestroyDefaults):
    """Deploy UC SQL functions declared under ``<instance>/sql_functions/``.

    Each direct subdirectory (not starting with ``_``) is treated as a
    sub-instance whose ``functions.sql`` + ``manifest.yml`` describe a
    set of UC functions.
    """

    name = "sql_function"
    display_name = "SQL Functions"
    # Ordering matches the legacy ``BRICK_DEPLOY_ORDER`` slot (SQL
    # Warehouse at 10, Data Domains at 20, SQL Functions at 30, ...).
    # Spacing by 10 leaves room for third-party bricks to insert.
    deploy_order = 30
    # State is a single ``deployed_functions`` list; no schema to validate.
    state_schema: type[BaseModel] | None = None

    def applies_to(self, ctx: DeployContext) -> bool:
        fn_dir = ctx.instance_dir / "sql_functions"
        if not fn_dir.is_dir():
            return False
        return any(d.is_dir() and not d.name.startswith("_") for d in fn_dir.iterdir())

    def deploy(self, ctx: DeployContext) -> list[BrickResult]:
        from bricks.sql_function.scripts.deploy_functions import deploy as deploy_fns

        fn_dir = ctx.instance_dir / "sql_functions"
        results: list[BrickResult] = []
        for sub in sorted(d for d in fn_dir.iterdir() if d.is_dir() and not d.name.startswith("_")):
            print(f"\n  [{sub.name}] Deploying SQL functions...")
            rc = deploy_fns(sub, ctx.target, ctx.variables, dry_run=ctx.dry_run, ws=ctx.ws)
            results.append(
                BrickResult(
                    name=sub.name,
                    status="ok" if rc == 0 else "deploy_error",
                    rc=rc,
                )
            )
        return results

    def load_state_outputs(self, ctx: DeployContext) -> dict[str, str]:  # noqa: ARG002
        return {}

    def plan_destroy(self, ctx: DestroyContext) -> DestroyPlan:
        from bricks.sql_function.scripts.deploy_functions import load_state

        items: list[DestroyPlanItem] = []
        for sub in iter_nested_sub_instances(ctx.instance_dir, "sql_functions"):
            functions = load_state(ctx.ws, sub, ctx.target).get("deployed_functions", [])
            if not functions:
                continue
            for fn in functions:
                items.append(
                    DestroyPlanItem(
                        brick=self.name,
                        sub_instance=sub.name,
                        resource=f"UC function {fn}",
                        destructive=True,
                        reason="drops SQL function",
                    )
                )
            items.append(
                DestroyPlanItem(
                    brick=self.name,
                    sub_instance=sub.name,
                    resource="UC schema (if empty after function drop)",
                    destructive=True,
                    reason="drops the schema sql_function created (RESTRICT -- preserves other tables/functions)",
                )
            )
        return DestroyPlan(items=items)

    def destroy(self, ctx: DestroyContext) -> list[BrickResult]:
        from bricks.sql_function.scripts.deploy_functions import destroy as destroy_fns
        from bricks.sql_function.scripts.deploy_functions import functions_to_drop, load_state

        results: list[BrickResult] = []
        for sub in iter_nested_sub_instances(ctx.instance_dir, "sql_functions"):
            if not ctx.force:
                if load_state(ctx.ws, sub, ctx.target):
                    results.append(
                        BrickResult(
                            name=sub.name,
                            status="skipped",
                            rc=0,
                            details={"reason": "requires --force"},
                        )
                    )
                continue
            functions = functions_to_drop(sub, ctx.target, ctx.variables, ws=ctx.ws)
            rc = destroy_fns(sub, ctx.target, ctx.variables, dry_run=ctx.dry_run, force=True, ws=ctx.ws)
            if rc == 0 and not functions:
                results.append(nothing_to_destroy(sub.name))
                continue
            results.append(
                BrickResult(
                    name=sub.name,
                    status="ok" if rc == 0 else "destroy_error",
                    rc=rc,
                    deleted=deleted_refs(ctx, rc, "uc_function", *functions),
                )
            )
        return results


# Runtime check: the adapter must satisfy the Brick protocol.  Running
# this at import time is cheap and catches drift immediately.
_: Brick = SqlFunctionBrick()
