"""Tool-source checks of the agent payload (Unity Gateway vs. local trees).

With ``spec.runtime.tools.skills.repository.enabled: false`` (or the
deprecated ``source: unity_gateway``) the ``skills/`` authoring tree is
never read, so leaving content in it is a configuration error rather
than something to ignore silently.  With repository and Unity Catalog
skills both on, a skill name may come from only one of them.

MCP is presence-based instead: ``mcp_servers/*/server.yml`` files and a
``mcp.gateway`` block both mount, so there is nothing dormant to report.
``mcp.source`` and the old skills fields are deprecated and only warned about.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent_foundry._instance_layout import resolve_payload_subdir

if TYPE_CHECKING:
    from pathlib import Path

    from payloads.agent.contracts import AgentPayloadSpec


def _authored_skill_dirs(instance_dir: Path) -> list[Path]:
    """Skill / domain dirs, skipping ``_``-prefixed build output like ``_assembled``."""
    skills_dir = resolve_payload_subdir(instance_dir, "skills")
    if not skills_dir.is_dir():
        return []
    return sorted(child for child in skills_dir.iterdir() if child.is_dir() and not child.name.startswith("_"))


def server_ymls(instance_dir: Path) -> list[Path]:
    """The ``mcp_servers/*/server.yml`` files that mount workspace MCP servers."""
    mcp_dir = resolve_payload_subdir(instance_dir, "mcp_servers")
    if not mcp_dir.is_dir():
        return []
    return sorted(mcp_dir.glob("*/server.yml"))


def dormant_source_dirs(instance_dir: Path, spec: AgentPayloadSpec) -> list[str]:
    """Return one error per authored path that the disabled repository source would ignore."""
    if spec.runtime.tools.skills.repository_enabled():
        return []
    return [
        f"{path} is ignored while repository skills are off "
        "(spec.runtime.tools.skills.repository.enabled: false or source: unity_gateway); remove it"
        for path in _authored_skill_dirs(instance_dir)
    ]


def duplicate_skill_names(instance_dir: Path, spec: AgentPayloadSpec) -> list[str]:
    """Return one error per skill folder that a pinned Unity Catalog skill also names."""
    skills = spec.runtime.tools.skills
    gateway = skills.effective_gateway()
    if not skills.repository_enabled() or gateway is None:
        return []
    pinned = {pin.fqn.rsplit(".", 1)[-1]: pin.fqn for pin in gateway.pinned}
    return [
        f"skill {path.name!r} comes from both {path} and Unity Catalog {pinned[path.name]}; rename or remove one"
        for path in _authored_skill_dirs(instance_dir)
        if path.name in pinned
    ]


def warn_deprecated_mcp_source(spec: AgentPayloadSpec) -> None:
    if not spec.runtime.tools.mcp.source_is_deprecated():
        return
    print(
        "  WARNING: spec.runtime.tools.mcp.source is deprecated and will be removed: "
        "mcp_servers/*/server.yml files and a mcp.gateway block both mount on their own."
    )


def warn_deprecated_skills_fields(spec: AgentPayloadSpec) -> None:
    deprecated = spec.runtime.tools.skills.deprecated_fields()
    if not deprecated:
        return
    print(
        f"  WARNING: spec.runtime.tools.skills {', '.join(deprecated)} is deprecated and will be removed: "
        "use skills.repository, skills.unity_catalog and skills.user_skills.enabled / store."
    )
