"""The agent payload's deploy-time provisioning manifest (issue #338).

Skills, MCP, and Lakebase chat history are *capabilities* of the agent
payload, not peer bricks.  Their deploy-time steps run inside the
payload's ``provision(ctx)`` hook in a small, fixed, payload-owned
order; this module expresses that order as data -- an explicit ordered
tuple behind a tiny :class:`ProvisionStep` protocol -- rather than as
buried imperative calls.

``AgentPayloadAdapter.provision`` iterates :data:`PROVISION_STEPS` in
order (each step mutates ``ctx.variables`` in place when it publishes
outputs); ``plan_destroy`` / ``destroy`` iterate the **reverse** order so
teardown unwinds provisioning.  Keeping the order here makes it
reviewable, reorderable, and unit-testable.

A declarative external manifest (e.g. ``payloads/agent/assembly.toml``)
is deferred until payload selection is config-driven -- it rides with
the future ``foundry.toml`` payload-selection slice.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from payloads.agent.deploy.chat_history import LakebaseStep
from payloads.agent.deploy.mcp import McpStep
from payloads.agent.deploy.skills import SkillsStep

if TYPE_CHECKING:
    from agent_foundry.bricks import (
        BrickResult,
        DeployContext,
        DestroyContext,
        DestroyPlanItem,
    )
    from payloads.agent.contracts import AgentPayloadSpec


@runtime_checkable
class ProvisionStep(Protocol):
    """One ordered deploy-time sub-step of the agent payload.

    Steps run within the App brick's slot during ``provision`` (the
    capabilities they own used to be standalone bricks).  ``provision``
    returns a process-style return code (``0`` == success) and may
    mutate ``ctx.variables`` in place to publish outputs that later
    steps -- or the App's ``app.yaml`` reconcile -- substitute.
    ``plan_destroy`` / ``destroy`` mirror the :class:`~agent_foundry.bricks.Brick`
    teardown contract but operate per-step.
    """

    name: str

    def provision(self, ctx: DeployContext) -> int: ...

    def plan_destroy(self, ctx: DestroyContext) -> list[DestroyPlanItem]: ...

    def destroy(self, ctx: DestroyContext) -> list[BrickResult]: ...


# Assembly order (issue #338).  Skills first (no cross-step coupling),
# then MCP -- which MUST run before the App's ``app.yaml`` render so its
# ``mcp__<sub>`` variables are in the substitution bag -- then chat
# history.  Reverse order tears these down cleanly on destroy.
PROVISION_STEPS: tuple[ProvisionStep, ...] = (
    SkillsStep(),
    McpStep(),
    LakebaseStep(),
)


def active_provision_steps(spec: AgentPayloadSpec) -> tuple[ProvisionStep, ...]:
    """The steps *spec* provisions: the skills step runs while repository skills are on.

    The MCP step always runs: it self-gates on the ``mcp_servers/`` tree,
    which may sit next to a ``mcp.gateway`` block.  Teardown still walks
    every step in :data:`PROVISION_STEPS`, so state left over from before
    repository skills were switched off is removed.
    """
    if not spec.runtime.tools.skills.repository_enabled():
        return tuple(step for step in PROVISION_STEPS if step.name != SkillsStep.name)
    return PROVISION_STEPS
