"""Data Domains brick adapter (issue #84, 68-C)."""

from __future__ import annotations

from pathlib import Path

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
from agent_foundry.destroy_base import iter_nested_sub_instances, nothing_to_destroy, uc_object_exists
from agent_foundry.variables import substitute
from bricks.data.scripts.deploy_views import load_domain_config


def qualified_schema(sub: Path, variables: dict[str, str]) -> str:
    """``catalog.schema`` a data domain deploys into, resolved like the deploy does."""
    domain = load_domain_config(sub).get("domain", {})
    domain_catalog = domain.get("catalog", "${var.catalog}")
    domain_schema = domain.get("schema", "${var.schema}")
    catalog = substitute(domain_catalog, {**variables}) if "${" in domain_catalog else domain_catalog
    schema = substitute(domain_schema, {**variables}) if "${" in domain_schema else domain_schema
    return f"{catalog}.{schema}"


class DataDomainsBrick(BrickDestroyDefaults):
    name = "data_domains"
    display_name = "Data Domains"
    deploy_order = 20
    state_schema: type[BaseModel] | None = None

    def applies_to(self, ctx: DeployContext) -> bool:
        d = ctx.instance_dir / "data_domains"
        return d.is_dir() and any(
            child.is_dir() and not child.name.startswith("_") and (child / "domain.yml").exists()
            for child in d.iterdir()
        )

    def deploy(self, ctx: DeployContext) -> list[BrickResult]:
        from bricks.data.scripts.deploy_views import deploy as deploy_domain

        data_dir = ctx.instance_dir / "data_domains"
        results: list[BrickResult] = []
        for sub in sorted(d for d in data_dir.iterdir() if d.is_dir() and not d.name.startswith("_")):
            if not (sub / "domain.yml").exists():
                continue
            print(f"\n  [{sub.name}] Deploying data domain...")
            rc = deploy_domain(sub, ctx.target, ctx.variables, dry_run=ctx.dry_run, ws=ctx.ws)
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
        items: list[DestroyPlanItem] = []
        for sub in iter_nested_sub_instances(ctx.instance_dir, "data_domains"):
            if not (sub / "domain.yml").exists():
                continue
            schema = qualified_schema(sub, ctx.variables)
            items.append(
                DestroyPlanItem(
                    brick=self.name,
                    sub_instance=sub.name,
                    resource=f"UC schema {schema}",
                    destructive=True,
                    reason="drops views and tables (CASCADE)",
                )
            )
        return DestroyPlan(items=items)

    def destroy(self, ctx: DestroyContext) -> list[BrickResult]:
        from bricks.data.scripts.deploy_views import destroy as destroy_domain

        results: list[BrickResult] = []
        for sub in iter_nested_sub_instances(ctx.instance_dir, "data_domains"):
            if not (sub / "domain.yml").exists():
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
            if ctx.ws is not None and not uc_object_exists(ctx.ws.schemas.get, qualified_schema(sub, ctx.variables)):
                results.append(nothing_to_destroy(sub.name))
                continue
            rc = destroy_domain(sub, ctx.target, ctx.variables, dry_run=ctx.dry_run, force=True, ws=ctx.ws)
            results.append(
                BrickResult(
                    name=sub.name,
                    status="ok" if rc == 0 else "destroy_error",
                    rc=rc,
                    deleted=deleted_refs(ctx, rc, "uc_schema", qualified_schema(sub, ctx.variables)),
                )
            )
        return results


_: Brick = DataDomainsBrick()
