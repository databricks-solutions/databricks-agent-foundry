"""Warn when ``ai_gateway`` brick skills are deployed but the agent never reads them.

The brick does not auto-pin: an agent reaches a brick skill only through a
pinned FQN or a discovery / user-catalog location covering its schema.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from payloads.agent.contracts import AgentPayloadSpec, GatewaySkillsRuntime


def _reachable_schemas(gateway: GatewaySkillsRuntime) -> set[str]:
    return {
        location for block in (gateway.discovery, gateway.user_catalog) if block.enabled for location in block.locations
    }


def unreachable_brick_skills(instance_dir: Path, spec: AgentPayloadSpec, variables: Mapping[str, str]) -> list[str]:
    """FQNs of brick skills neither pinned nor inside a discovery location.

    An invalid brick config yields none here: the brick reports it itself.
    """
    from bricks.ai_gateway.config import load_config, skill_dirs

    try:
        section = load_config(instance_dir, dict(variables)).skills
    except ValueError:
        return []
    if section is None:
        return []
    fqns = [f"{section.schema_fqn}.{folder.name}" for folder in skill_dirs(instance_dir)]
    gateway = spec.runtime.tools.skills.effective_gateway()
    if gateway is None:
        return fqns
    pinned = {entry.fqn for entry in gateway.pinned}
    schemas = _reachable_schemas(gateway)
    return [fqn for fqn in fqns if fqn not in pinned and fqn.rsplit(".", 1)[0] not in schemas]


def warn_unreachable_brick_skills(instance_dir: Path, spec: AgentPayloadSpec, variables: Mapping[str, str]) -> None:
    for fqn in unreachable_brick_skills(instance_dir, spec, variables):
        print(
            f"  WARNING: ai_gateway skill {fqn} is not reachable by the agent: pin it or add its schema "
            "to runtime.tools.skills.unity_catalog.discovery or user_skills.locations"
        )


__all__ = ["unreachable_brick_skills", "warn_unreachable_brick_skills"]
