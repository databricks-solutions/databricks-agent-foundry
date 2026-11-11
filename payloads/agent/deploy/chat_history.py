"""Chat-history capability glue for the agent payload (issue #338).

Chat history is a *capability* of the agent payload (a Lakebase
Autoscaling project backing the App's ``postgres`` ``app_resource``), not
a peer brick.  This module owns the capability's gating and its state
contract so both the App brick's ``load_state_outputs`` and the
agent payload's ``provision`` / ``plan_destroy`` / ``destroy`` hooks read
it from one place.

State lives in the shared workspace state (``agent_foundry.state``, type
``lakebase``).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agent_foundry.bricks import (
        BrickResult,
        DeployContext,
        DestroyContext,
        DestroyPlanItem,
    )

# Same key map as the retired ``LakebaseBrick`` so re-deploys read
# existing workspace state without a forced rebuild.
LAKEBASE_STATE_KEYS = {
    "lakebase_project_name": "lakebase_project_name",
    "lakebase_branch_path": "lakebase_branch_path",
    "lakebase_database_path": "lakebase_database_path",
}


def chat_history_enabled(ctx: DeployContext) -> bool:
    """Read the chat-history toggle from the deploy variable bag.

    The agent payload's ``provision`` publishes ``chat_history_enabled``
    (derived from ``spec.ui.chat_history.enabled``) into ``ctx.variables``
    before the provision steps run -- that bag is the post-#338 source of
    truth.  ``ctx.config`` is the framework ``InstanceConfig``, which no
    longer carries the (agent-owned) chat-history toggle, so reading it
    here raised ``AttributeError`` on every agent deploy.  Defaults to
    True when the key is absent (hermetic / destroy paths) -- matches the
    default-on authoring contract.
    """
    return ctx.variables.get("chat_history_enabled", "true") == "true"


def load_lakebase_state(instance_dir: Path, target: str, ws: Any | None = None) -> dict[str, Any]:
    """Read the shared ``lakebase`` workspace state.

    Returns an empty dict when there is no state (e.g. first deploy of
    a fresh project) -- callers must tolerate that.  Validation against
    :class:`~payloads.agent.deploy.lakebase.LakebaseState` runs only when
    state is non-empty so post-destroy / never-deployed instances do not
    flag.
    """
    from payloads.agent.deploy.lakebase import LakebaseState, load_state

    state = load_state(ws, instance_dir, target)
    if state:
        from agent_foundry.state import validate_state

        validate_state("app", state, LakebaseState)
    return state


class LakebaseStep:
    """Provision / tear down the Lakebase chat-history project (issue #338).

    The chat-history :class:`ProvisionStep
    <payloads.agent.deploy.assembly.ProvisionStep>` in the agent payload's
    manifest.  Runs after skills and before the App deploy so the
    discovered project / branch / database paths land in
    ``ctx.variables`` before the ``postgres`` ``app_resource`` template
    resolves.  Every method is a no-op when chat history is disabled.
    """

    name = "lakebase"

    def provision(self, ctx: DeployContext) -> int:
        """Provision the Lakebase project; no-op when chat history is off."""
        if not chat_history_enabled(ctx):
            print(f"\n  [{ctx.instance_dir.name}] Skipping Lakebase (chat_history.enabled=false)")
            return 0

        from payloads.agent.deploy.lakebase import provision_lakebase_project

        print(f"\n  [{ctx.instance_dir.name}] Provisioning Lakebase project (chat_history.enabled=true)...")
        return provision_lakebase_project(
            ctx.instance_dir,
            ctx.target,
            ctx.variables,
            dry_run=ctx.dry_run,
            ws=ctx.ws,
        )

    def plan_destroy(self, ctx: DestroyContext) -> list[DestroyPlanItem]:
        """Plan the Lakebase teardown item (when chat history is enabled)."""
        from agent_foundry.bricks import DestroyPlanItem

        if not chat_history_enabled(ctx):
            return []

        from payloads.agent.deploy.lakebase import lakebase_project_name

        state = load_lakebase_state(ctx.instance_dir, ctx.target, ctx.ws)
        if not (state.get("lakebase_project_name") or ctx.variables.get("lakebase_project_name")):
            return []
        project = lakebase_project_name(ctx.instance_dir, ctx.target, ctx.variables, ws=ctx.ws)
        # ``brick="app"``: the agent payload only runs under the App brick.
        return [
            DestroyPlanItem(
                brick="app",
                sub_instance="lakebase",
                resource=f"Lakebase project {project}",
                destructive=True,
                reason="deletes chat history database",
            )
        ]

    def destroy(self, ctx: DestroyContext) -> list[BrickResult]:
        """Tear down the Lakebase project (guarded by ``--force``)."""
        from agent_foundry.bricks import BrickResult, deleted_refs

        if not chat_history_enabled(ctx):
            return []

        state = load_lakebase_state(ctx.instance_dir, ctx.target, ctx.ws)
        if not state.get("lakebase_project_name"):
            return []

        if not ctx.force:
            return [
                BrickResult(
                    name="lakebase",
                    status="skipped",
                    rc=0,
                    details={"reason": "requires --force", "resource": "Lakebase project"},
                )
            ]

        from payloads.agent.deploy.lakebase import destroy_lakebase_project, lakebase_project_name

        project_path = f"projects/{lakebase_project_name(ctx.instance_dir, ctx.target, ctx.variables, ws=ctx.ws)}"
        lb_rc = destroy_lakebase_project(
            ctx.instance_dir,
            ctx.target,
            ctx.variables,
            dry_run=ctx.dry_run,
            ws=ctx.ws,
        )
        return [
            BrickResult(
                name="lakebase",
                status="ok" if lb_rc == 0 else "destroy_error",
                rc=lb_rc,
                deleted=deleted_refs(ctx, lb_rc, "lakebase_project", project_path),
            )
        ]
