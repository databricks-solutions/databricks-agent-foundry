"""Import UI-edited skills back into git."""

from __future__ import annotations

from pathlib import Path

import pytest

from bricks.ai_gateway.scripts.import_skills import import_skills
from bricks.ai_gateway.scripts.skills import deploy_skills
from bricks.ai_gateway.tests.unit.conftest import SCHEMA, FakeWorkspace, skill_md, write_skill

VARS = {"catalog": "main"}
ROOT = "/Skills/main/skills/triage"


def _remote_skill(ws: FakeWorkspace, files: dict[str, str]) -> None:
    ws.ai_gateway.skills[f"skills/{SCHEMA}.triage"] = "etag-ui"
    for rel, text in files.items():
        ws.file_store[f"{ROOT}/{rel}"] = text.encode()


@pytest.mark.usefixtures("state_store")
def test_import_mirrors_the_remote_bundle(instance_dir: Path, ws: FakeWorkspace) -> None:
    folder = write_skill(instance_dir, "triage", {"SKILL.md": skill_md("triage"), "stale.md": "old"})
    _remote_skill(ws, {"SKILL.md": skill_md("triage", body="UI edit."), "refs/new.md": "N"})
    assert import_skills(instance_dir, VARS, ws) == 0  # type: ignore[arg-type]
    assert sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file()) == [
        "SKILL.md",
        "refs/new.md",
    ]
    assert "UI edit." in (folder / "SKILL.md").read_text(encoding="utf-8")


@pytest.mark.usefixtures("state_store")
def test_deploy_after_import_uploads_nothing(instance_dir: Path, ws: FakeWorkspace) -> None:
    _remote_skill(ws, {"SKILL.md": skill_md("triage", body="UI edit.")})
    import_skills(instance_dir, VARS, ws)  # type: ignore[arg-type]
    report = deploy_skills(instance_dir, "dev", VARS, ws=ws)  # type: ignore[arg-type]
    assert report.changed == [] and ws.ai_gateway.calls == []


@pytest.mark.usefixtures("state_store")
def test_import_refuses_paths_outside_the_skill_folder(instance_dir: Path, ws: FakeWorkspace) -> None:
    _remote_skill(ws, {"SKILL.md": skill_md("triage"), "../evil.md": "x"})
    assert import_skills(instance_dir, VARS, ws, ["triage"]) == 1  # type: ignore[arg-type]
    assert not (instance_dir / "ai_gateway" / "skills" / "evil.md").exists()
    assert not (instance_dir / "ai_gateway" / "skills" / "triage").exists()


@pytest.mark.usefixtures("state_store")
def test_import_refuses_an_invalid_remote_skill(instance_dir: Path, ws: FakeWorkspace) -> None:
    folder = write_skill(instance_dir, "triage")
    _remote_skill(ws, {"SKILL.md": "no frontmatter"})
    assert import_skills(instance_dir, VARS, ws) == 1  # type: ignore[arg-type]
    assert (folder / "SKILL.md").read_text(encoding="utf-8") == skill_md("triage")
