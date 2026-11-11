"""Typed contracts for skills.

``SKILL.md`` (agentskills.io frontmatter ``name`` + ``description``, then a
markdown body) is the one skill format shared by repo skills, Unity Catalog
skills, user-authored gateway skills and Lakebase user skills.  Its rules live
here so every source enforces the same ones; size caps stay with each source.

Legacy repo-skill YAML, read for one more release:

- ``skill.yml`` -> :class:`SkillMetadata`
- ``domain.yml`` -> :class:`SkillDomainMetadata`
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

import yaml
from pydantic import BaseModel, ConfigDict

SKILL_FILE: Final = "SKILL.md"
# Unanchored so callers can compose it, e.g. ``lakebase:<slug>``.
SKILL_SLUG: Final = r"[a-z0-9]+(?:-[a-z0-9]+)*"
MAX_SKILL_ID_CHARS: Final = 64
MAX_SKILL_DESCRIPTION_CHARS: Final = 1024

_SLUG = re.compile(rf"^{SKILL_SLUG}$")
_FRONTMATTER = re.compile(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|\Z)", re.DOTALL)


class SkillDocumentError(ValueError):
    """A ``SKILL.md`` text breaks the skill contract."""


@dataclass(frozen=True)
class SkillDocument:
    """A parsed ``SKILL.md``: frontmatter ``name`` and ``description``, and the body."""

    name: str
    description: str
    body: str


def is_skill_slug(value: str, max_chars: int = MAX_SKILL_ID_CHARS) -> bool:
    return len(value) <= max_chars and _SLUG.fullmatch(value) is not None


def _frontmatter_text(meta: dict[object, object], key: str) -> str:
    value = meta.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SkillDocumentError(f"frontmatter needs a non-empty {key}")
    return value.strip()


def parse_skill_md(text: str) -> SkillDocument:
    """Split *text* into frontmatter and body; the body may be empty."""
    match = _FRONTMATTER.match(text)
    try:
        meta = yaml.safe_load(match.group(1)) if match else None
    except yaml.YAMLError as exc:
        raise SkillDocumentError(f"{SKILL_FILE} frontmatter is not valid YAML") from exc
    if match is None or not isinstance(meta, dict):
        raise SkillDocumentError(f"{SKILL_FILE} needs '---' YAML frontmatter")
    description = _frontmatter_text(meta, "description")
    if len(description) > MAX_SKILL_DESCRIPTION_CHARS:
        raise SkillDocumentError(f"description is over {MAX_SKILL_DESCRIPTION_CHARS} chars")
    return SkillDocument(
        name=_frontmatter_text(meta, "name"),
        description=description,
        body=text[match.end() :].strip(),
    )


def render_skill_md(name: str, description: str, body: str) -> str:
    """``SKILL.md`` with generated frontmatter; the body cannot reach the YAML."""
    frontmatter = yaml.safe_dump(
        {"name": name, "description": description},
        sort_keys=False,
        allow_unicode=True,
        width=1_000_000,
    )
    return f"---\n{frontmatter}---\n\n{body.strip()}\n"


_MODEL_CONFIG = ConfigDict(extra="ignore")


class SkillMetadata(BaseModel):
    """Contents of ``skills/<domain>/<skill>/skill.yml``.

    ``tools`` here is a list of tool *names* (references into the skill's
    ``tools.yml``), not the full tool objects -- those are in
    :class:`agent_foundry.contracts.tools.ToolManifest`.
    """

    model_config = _MODEL_CONFIG

    name: str
    description: str = ""
    triggers: list[str] = []
    phase: int = 999
    tools: list[str] = []


class SkillDomainMetadata(BaseModel):
    """Contents of ``skills/<domain>/domain.yml``."""

    model_config = _MODEL_CONFIG

    name: str
    description: str = ""
    business_process: str = ""


__all__ = [
    "MAX_SKILL_DESCRIPTION_CHARS",
    "MAX_SKILL_ID_CHARS",
    "SKILL_FILE",
    "SKILL_SLUG",
    "SkillDocument",
    "SkillDocumentError",
    "SkillDomainMetadata",
    "SkillMetadata",
    "is_skill_slug",
    "parse_skill_md",
    "render_skill_md",
]
