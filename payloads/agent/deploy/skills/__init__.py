"""Agent-payload skills capability (issue #338).

Owns the deploy-time skill surface for the agent payload: the
``SkillsStep`` provision step plus the validation / assembly
implementation it drives (:mod:`payloads.agent.deploy.skills.validate`,
:mod:`payloads.agent.deploy.skills.assemble`).
"""

from __future__ import annotations

from payloads.agent.deploy.skills.step import SkillsStep

__all__ = ["SkillsStep"]
