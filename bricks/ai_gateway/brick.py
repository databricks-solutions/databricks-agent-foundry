"""Unity Gateway artifacts brick: git-managed resources under ``<instance>/ai_gateway/``.

Named for the gateway rather than one artifact kind: each kind is one
optional ``config.yml`` section, one subfolder and one script module.
v1 ships ``skills`` (:mod:`bricks.ai_gateway.scripts.skills`).
"""

from __future__ import annotations

from pydantic import BaseModel, ValidationError

from agent_foundry.bricks import (
    Brick,
    BrickDestroyDefaults,
    BrickResult,
    DeployContext,
    DestroyContext,
    DestroyPlan,
    DestroyPlanItem,
    ResourceRef,
    deleted_refs,
)
from agent_foundry.destroy_base import nothing_to_destroy
from bricks.ai_gateway.config import has_skills_section, skill_dirs

_SKILLS = "skills"


class AiGatewayBrick(BrickDestroyDefaults):
    name = "ai_gateway"
    display_name = "Unity Gateway"
    # After data and functions, before the App (90) that consumes the skills.
    deploy_order = 80
    state_schema: type[BaseModel] | None = None

    def applies_to(self, ctx: DeployContext) -> bool:
        return has_skills_section(ctx.instance_dir) and bool(skill_dirs(ctx.instance_dir))

    def deploy(self, ctx: DeployContext) -> list[BrickResult]:
        from bricks.ai_gateway.scripts.skills import deploy_skills

        print(f"\n  [{ctx.instance_dir.name}] Syncing Unity Gateway skills...")
        try:
            report = deploy_skills(ctx.instance_dir, ctx.target, ctx.variables, dry_run=ctx.dry_run, ws=ctx.ws)
        except (ValidationError, ValueError) as exc:
            print(f"  [skills] CONFIG INVALID: {exc}")
            return [BrickResult(name=_SKILLS, status="validate_error", rc=1, details={"error": str(exc)})]
        status = "ok" if report.rc == 0 else "deploy_error"
        return [BrickResult(name=_SKILLS, status=status, rc=report.rc, details=report.details())]

    def load_state_outputs(self, ctx: DeployContext) -> dict[str, str]:  # noqa: ARG002
        return {}

    def plan_destroy(self, ctx: DestroyContext) -> DestroyPlan:
        from bricks.ai_gateway.scripts.skills import load_skills_state

        state = load_skills_state(ctx.ws, ctx.instance_dir, ctx.target)
        items = [
            DestroyPlanItem(brick=self.name, sub_instance=_SKILLS, resource=f"UC skill {fqn}", destructive=False)
            for fqn in sorted(state.skills)
        ]
        items += [
            DestroyPlanItem(
                brick=self.name,
                sub_instance=_SKILLS,
                resource=f"UC schema {schema} (if empty)",
                destructive=False,
                reason="the brick created it; a non-empty schema is preserved",
            )
            for schema in state.created_schemas
        ]
        return DestroyPlan(items=items)

    def destroy(self, ctx: DestroyContext) -> list[BrickResult]:
        from bricks.ai_gateway.scripts.skills import destroy_skills, load_skills_state

        state = load_skills_state(ctx.ws, ctx.instance_dir, ctx.target)
        if not state.skills and not state.created_schemas:
            return [nothing_to_destroy(_SKILLS)]
        outcome = destroy_skills(ctx.instance_dir, ctx.target, dry_run=ctx.dry_run, ws=ctx.ws)
        deleted: list[ResourceRef] = [
            *deleted_refs(ctx, outcome.rc, "skill", *outcome.deleted_skills),
            *deleted_refs(ctx, outcome.rc, "uc_schema", *outcome.dropped_schemas),
        ]
        return [
            BrickResult(
                name=_SKILLS,
                status="ok" if outcome.rc == 0 else "destroy_error",
                rc=outcome.rc,
                deleted=deleted,
            )
        ]


_: Brick = AiGatewayBrick()
