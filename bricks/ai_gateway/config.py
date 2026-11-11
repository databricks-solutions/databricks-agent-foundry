"""``<instance>/ai_gateway/`` layout and its typed ``config.yml``.

One optional section per Unity Gateway artifact kind; v1 has ``skills``
only.  ``${var.*}`` placeholders are substituted before validation, so
the validated names are always concrete Unity Catalog names.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import yaml
from pydantic import BaseModel, ConfigDict, Field

BRICK_DIR: Final = "ai_gateway"
CONFIG_FILE: Final = "config.yml"
SKILLS_DIR: Final = "skills"

_UC_NAME = r"[A-Za-z0-9_][A-Za-z0-9_-]*"
SCHEMA_FQN_PATTERN: Final = rf"^{_UC_NAME}\.{_UC_NAME}$"


class SkillsSection(BaseModel):
    """``skills:`` -- the schema every skill folder is deployed into."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    schema_fqn: str = Field(alias="schema", pattern=SCHEMA_FQN_PATTERN)


class AiGatewayConfig(BaseModel):
    """``<instance>/ai_gateway/config.yml``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    skills: SkillsSection | None = None


def brick_dir(instance_dir: Path) -> Path:
    return instance_dir / BRICK_DIR


def skills_root(instance_dir: Path) -> Path:
    return brick_dir(instance_dir) / SKILLS_DIR


def skill_dirs(instance_dir: Path) -> list[Path]:
    """Skill folders under ``ai_gateway/skills/``; ``_`` and ``.`` prefixes are ignored."""
    root = skills_root(instance_dir)
    if not root.is_dir():
        return []
    return sorted(d for d in root.iterdir() if d.is_dir() and not d.name.startswith(("_", ".")))


def has_skills_section(instance_dir: Path) -> bool:
    """``config.yml`` declares ``skills`` (read raw: no variables needed)."""
    path = brick_dir(instance_dir) / CONFIG_FILE
    if not path.is_file():
        return False
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return isinstance(raw, dict) and raw.get("skills") is not None


def load_config(instance_dir: Path, variables: dict[str, str]) -> AiGatewayConfig:
    """The validated config; an absent file means no sections.

    Raises ``ValueError`` on an undefined variable and
    ``pydantic.ValidationError`` on a malformed file.
    """
    from agent_foundry.variables import substitute_obj

    path = brick_dir(instance_dir) / CONFIG_FILE
    if not path.is_file():
        return AiGatewayConfig()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return AiGatewayConfig.model_validate(substitute_obj(raw, variables))


__all__ = [
    "BRICK_DIR",
    "CONFIG_FILE",
    "SCHEMA_FQN_PATTERN",
    "SKILLS_DIR",
    "AiGatewayConfig",
    "SkillsSection",
    "brick_dir",
    "has_skills_section",
    "load_config",
    "skill_dirs",
    "skills_root",
]
