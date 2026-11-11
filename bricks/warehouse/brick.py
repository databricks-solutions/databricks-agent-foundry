"""SQL Warehouse brick adapter (issue #84, 68-C)."""

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
from agent_foundry.destroy_base import nothing_to_destroy


class WarehouseState(BaseModel):
    """Contract for the instance's ``warehouse`` workspace state.

    Issue #156 pilot: the SQL Warehouse brick is the first to declare a
    typed schema for its persisted state file.  The orchestrator
    (via :class:`agent_foundry.bricks.StandardLayoutBrick.load_state_outputs`)
    validates each non-empty state file against this schema on read,
    so contract violations surface as
    :class:`agent_foundry.state.BrickStateValidationError` instead of
    silent missing-variable bugs in downstream bricks.

    Schema policy: ``extra="ignore"`` so future-version metadata
    (e.g. workspace ``_audit`` trails added later, additional fields)
    don't break validation for instances mid-rollout.  Required keys
    are still strictly checked.
    """

    model_config = ConfigDict(extra="ignore")

    warehouse_id: str
    name: str


class SqlWarehouseBrick(StandardLayoutBrick):
    name = "warehouse"
    display_name = "SQL Warehouse"
    deploy_order = 10
    state_schema: type[BaseModel] | None = WarehouseState

    # Flat layout: one workspace state per instance publishing the
    # provisioned warehouse_id.
    state_subdir = "warehouse"
    state_keys = {"warehouse_id": "warehouse_id"}
    is_nested_layout = False

    def load_state_outputs(self, ctx: DeployContext) -> dict[str, str]:
        """Publish ``warehouse_id`` from state, with a name-based fallback.

        Standard state lookup runs first (inherited from
        :class:`StandardLayoutBrick`, workspace only). When state is empty -- typical
        for destroy runs against a workspace whose state cache was
        wiped, or for a re-adopt after manual cleanup -- fall back to
        looking up the warehouse by configured name. This keeps the
        ``warehouse_id`` variable populated for downstream bricks
        without any cross-brick coupling: only the warehouse brick
        knows about ``warehouse/config.yml`` and the warehouse
        listing API.
        """
        outputs = super().load_state_outputs(ctx)
        if outputs.get("warehouse_id") or ctx.dry_run or ctx.ws is None:
            return outputs
        wh_id = self._discover_warehouse_by_name(ctx)
        if wh_id:
            outputs["warehouse_id"] = wh_id
        return outputs

    def _discover_warehouse_by_name(self, ctx: DeployContext) -> str:
        config_path = ctx.instance_dir / "warehouse" / "config.yml"
        if not config_path.exists():
            return ""
        try:
            import yaml

            from agent_foundry.variables import substitute

            raw = config_path.read_text(encoding="utf-8")
            substituted = substitute(raw, ctx.variables)
            config = yaml.safe_load(substituted) or {}
        except Exception:
            return ""

        name = config.get("name", "")
        if not name:
            return ""

        ws = ctx.ws
        if ws is None:
            return ""
        try:
            resp = ws.api_client.do("GET", "/api/2.0/sql/warehouses")
            for wh in resp.get("warehouses", []):
                if wh.get("name", "").lower() == name.lower():
                    return str(wh.get("id", ""))
        except Exception:
            pass
        return ""

    def applies_to(self, ctx: DeployContext) -> bool:
        # ``[warehouse] mode = "reuse"`` instances are skipped too: the
        # loader rejects reuse mode alongside a warehouse/config.yml.
        return (ctx.instance_dir / "warehouse" / "config.yml").exists()

    def deploy(self, ctx: DeployContext) -> list[BrickResult]:
        from bricks.warehouse.scripts.deploy_warehouse import (
            deploy as deploy_wh,
        )
        from bricks.warehouse.scripts.deploy_warehouse import (
            load_state as load_wh_state,
        )

        print(f"\n  [{ctx.instance_dir.name}] Deploying dedicated SQL warehouse...")
        rc = deploy_wh(ctx.instance_dir, ctx.target, ctx.variables, dry_run=ctx.dry_run, ws=ctx.ws)

        # Propagate the warehouse_id to downstream bricks in the same
        # run without waiting for the next _preload_state cycle.
        state = load_wh_state(ctx.ws, ctx.instance_dir, ctx.target)
        wh_id = state.get("warehouse_id")
        if wh_id:
            ctx.variables["warehouse_id"] = wh_id

        return [
            BrickResult(
                name=ctx.instance_dir.name,
                status="ok" if rc == 0 else "deploy_error",
                rc=rc,
            )
        ]

    def plan_destroy(self, ctx: DestroyContext) -> DestroyPlan:
        from bricks.warehouse.scripts.deploy_warehouse import load_state as load_wh_state

        state = load_wh_state(ctx.ws, ctx.instance_dir, ctx.target)
        wh_id = state.get("warehouse_id")
        if not wh_id:
            return DestroyPlan()
        name = state.get("name", wh_id)
        return DestroyPlan(
            items=[
                DestroyPlanItem(
                    brick=self.name,
                    resource=f"SQL warehouse {name} ({wh_id})",
                    destructive=False,
                )
            ]
        )

    def destroy(self, ctx: DestroyContext) -> list[BrickResult]:
        from bricks.warehouse.scripts.deploy_warehouse import deploy as deploy_wh
        from bricks.warehouse.scripts.deploy_warehouse import load_state as load_wh_state

        wh_id = load_wh_state(ctx.ws, ctx.instance_dir, ctx.target).get("warehouse_id")
        if not wh_id:
            return [nothing_to_destroy(ctx.instance_dir.name)]
        rc = deploy_wh(
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
                deleted=deleted_refs(ctx, rc, "sql_warehouse", wh_id),
            )
        ]


_: Brick = SqlWarehouseBrick()
