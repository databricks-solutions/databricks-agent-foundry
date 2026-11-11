"""The chat UI's TypeScript copies of the skill rules match the Python contract."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from agent_foundry.contracts.skills import MAX_SKILL_DESCRIPTION_CHARS, SKILL_SLUG
from payloads.agent.runtime.skill_authoring import MAX_SLUG_CHARS

_OVERRIDES = Path(__file__).resolve().parents[2] / "ui" / "overrides"
_TS_MIRRORS = [
    _OVERRIDES / "server" / "src" / "lib" / "user-skills.ts",
    _OVERRIDES / "client" / "src" / "lib" / "skill-draft.ts",
]


def _ts_constant(source: str, name: str) -> str:
    match = re.search(rf"^export const {name} = (.+);$", source, re.MULTILINE)
    assert match, f"{name} not found"
    return match.group(1)


@pytest.mark.parametrize("path", _TS_MIRRORS, ids=lambda p: p.name)
def test_ts_mirror_matches_the_contract(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    assert _ts_constant(source, "SLUG_PATTERN") == f"/^{SKILL_SLUG}$/"
    assert int(_ts_constant(source, "MAX_DESCRIPTION_CHARS")) == MAX_SKILL_DESCRIPTION_CHARS
    assert int(_ts_constant(source, "MAX_SLUG_CHARS")) == MAX_SLUG_CHARS
