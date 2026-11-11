"""Skills provisioning step for the agent payload (issue #338).

Folds the former standalone ``SkillsBrick`` (deploy_order 70) into the
agent payload's provision manifest: validate the instance's skills,
assemble the merged system prompt + tool manifest, and write the
``<instance>/skills/_assembled/`` artefacts the runtime consumes.

Runtime consumption is unchanged -- the runtime reads the assembled
files, never this step.  The validation / assembly implementation lives
alongside this step (:mod:`payloads.agent.deploy.skills.validate` and
:mod:`payloads.agent.deploy.skills.assemble`) and is imported lazily so
the manifest module stays cheap to construct.
"""

from __future__ import annotations

import json
import shutil
from typing import TYPE_CHECKING

from agent_foundry._instance_layout import resolve_payload_subdir

if TYPE_CHECKING:
    from pathlib import Path

    from agent_foundry.bricks import (
        BrickResult,
        DeployContext,
        DestroyContext,
        DestroyPlanItem,
    )


def _assembled_dir(instance_dir: Path) -> Path:
    """The payload-owned ``skills/_assembled/`` output dir (issue #338)."""
    return resolve_payload_subdir(instance_dir, "skills") / "_assembled"


class SkillsStep:
    """Validate + assemble the instance's skills into ``_assembled/``."""

    name = "skills"

    def _has_skills(self, ctx: DeployContext) -> bool:
        """True when the instance carries at least one skill / domain dir.

        Mirrors the former ``SkillsBrick.applies_to`` gate so the step
        is a no-op for instances without a ``skills/`` tree.
        """
        skills_dir = resolve_payload_subdir(ctx.instance_dir, "skills")
        if not skills_dir.is_dir():
            return False
        return any(child.is_dir() and not child.name.startswith("_") for child in skills_dir.iterdir())

    def provision(self, ctx: DeployContext) -> int:
        """Validate then assemble; returns ``1`` on validation failure.

        A non-zero return short-circuits the manifest and blocks the App
        deploy (issue #338): an instance with invalid skills must not
        ship.  The assembled artefacts are written before the App syncs
        source, so the runtime always sees a fresh ``_assembled/``.
        """
        if not self._has_skills(ctx):
            return 0

        from payloads.agent.deploy.skills.assemble import assemble_skills
        from payloads.agent.deploy.skills.validate import validate_instance_skills

        name = ctx.instance_dir.name
        print(f"\n  [{name}] Validating skills...")
        errors = validate_instance_skills(ctx.instance_dir)
        if any(e.severity == "error" for e in errors):
            print(f"  [{name}] SKILL VALIDATION FAILED:")
            for e in errors:
                print(f"    - {e}")
            return 1
        for e in errors:
            print(f"    - {e}")

        print(f"  [{name}] Assembling skills...")
        result = assemble_skills(ctx.instance_dir, ctx.variables or None)
        self._write_artefacts(ctx, result)

        n_skills = len(result["skill_names"])
        n_tools = len(result["tools"])
        skill_index = result.get("skill_index")
        mode_suffix = f", lazy mode ({len(skill_index)} skill_ids indexed)" if skill_index is not None else ""
        print(f"  [{name}] Skills OK ({n_skills} skills, {n_tools} tools{mode_suffix})")
        return 0

    def _write_artefacts(self, ctx: DeployContext, result: dict[str, object]) -> None:
        """Write the on-disk artefacts the runtime reads (issue #83, #98).

        ``system_prompt.md`` + ``skill_tools.json`` are produced
        unconditionally; ``skill_index.json`` only in lazy mode so the
        eager-only on-disk contract is unchanged.
        """
        out_dir = _assembled_dir(ctx.instance_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        (out_dir / "system_prompt.md").write_text(str(result["system_prompt"]), encoding="utf-8")
        (out_dir / "skill_tools.json").write_text(
            json.dumps(
                {
                    "tools": result["tools"],
                    "skill_names": result["skill_names"],
                    "domain_names": result["domain_names"],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        skill_index = result.get("skill_index")
        index_path = out_dir / "skill_index.json"
        if skill_index is not None:
            index_path.write_text(
                json.dumps(
                    {
                        "skill_loading": result.get("skill_loading", "lazy"),
                        "index": skill_index,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        else:
            # Switching lazy -> eager must remove the index, not just stop
            # writing it: the runtime treats its presence as the mode
            # signal, so a leftover file re-attaches ``_load_skill`` on top
            # of a prompt that already carries every skill body inline.
            index_path.unlink(missing_ok=True)

    def plan_destroy(self, ctx: DestroyContext) -> list[DestroyPlanItem]:
        """Plan removal of the local ``_assembled/`` tree (non-destructive)."""
        from agent_foundry.bricks import DestroyPlanItem

        assembled = _assembled_dir(ctx.instance_dir)
        if not assembled.is_dir():
            return []
        # ``brick="app"``: the agent payload only runs under the App brick.
        return [
            DestroyPlanItem(
                brick="app",
                sub_instance="skills",
                resource=f"local assembled skills at {assembled}",
                destructive=False,
            )
        ]

    def destroy(self, ctx: DestroyContext) -> list[BrickResult]:
        """Remove the local ``_assembled/`` tree (non-destructive, no --force)."""
        from agent_foundry.bricks import BrickResult

        assembled = _assembled_dir(ctx.instance_dir)
        if not assembled.is_dir():
            return []
        if ctx.dry_run:
            print(f"  [DRY RUN] Would remove {assembled}")
            return [BrickResult(name="skills", status="ok", rc=0)]
        shutil.rmtree(assembled)
        print(f"  Removed {assembled}")
        return [BrickResult(name="skills", status="ok", rc=0)]
