"""Skill folder validation, content hashing and bundle paths."""

from __future__ import annotations

from pathlib import Path

import pytest

from bricks.ai_gateway.skill_bundle import (
    MAX_SKILL_MD_CHARS,
    SkillBundleError,
    bundle_root,
    content_hash,
    read_local_skill,
    safe_relative_path,
)
from bricks.ai_gateway.tests.unit.conftest import skill_md, write_skill


def test_reads_skill_md_and_supporting_files(instance_dir: Path) -> None:
    folder = write_skill(instance_dir, "triage", {"SKILL.md": skill_md("triage"), "refs/a.md": "A", ".DS_Store": "x"})
    skill = read_local_skill(folder)
    assert skill.skill_id == "triage"
    assert sorted(skill.files) == ["SKILL.md", "refs/a.md"]


@pytest.mark.parametrize(
    ("folder", "text", "match"),
    [
        ("triage", "# no frontmatter\n", "frontmatter"),
        ("triage", skill_md("other"), "must equal the folder name"),
        ("triage", skill_md("triage", description="' '"), "non-empty description"),
        ("triage", skill_md("triage", body="x" * MAX_SKILL_MD_CHARS), "cap"),
        ("Triage", skill_md("Triage"), "lowercase"),
        ("tri--age", skill_md("tri--age"), "lowercase"),
        ("x" * 65, skill_md("x" * 65), "lowercase"),
    ],
)
def test_invalid_skill_folders_are_rejected(instance_dir: Path, folder: str, text: str, match: str) -> None:
    with pytest.raises(SkillBundleError, match=match):
        read_local_skill(write_skill(instance_dir, folder, {"SKILL.md": text}))


def test_symlinks_are_refused(instance_dir: Path, tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("token", encoding="utf-8")
    folder = write_skill(instance_dir, "triage")
    (folder / "leak.txt").symlink_to(secret)
    with pytest.raises(SkillBundleError, match="symlinks"):
        read_local_skill(folder)


def test_missing_skill_md_is_rejected(instance_dir: Path) -> None:
    with pytest.raises(SkillBundleError, match="missing SKILL.md"):
        read_local_skill(write_skill(instance_dir, "triage", {"notes.md": "x"}))


def test_content_hash_ignores_order_and_tracks_content() -> None:
    files = {"SKILL.md": b"a", "refs/b.md": b"b"}
    assert content_hash(files) == content_hash(dict(reversed(list(files.items()))))
    assert content_hash(files) != content_hash({**files, "refs/b.md": b"c"})
    assert content_hash({"ab": b"c"}) != content_hash({"a": b"bc"})


def test_bundle_root_refuses_anything_but_a_skill_fqn() -> None:
    assert bundle_root("main.skills.triage") == "/Skills/main/skills/triage"
    with pytest.raises(SkillBundleError):
        bundle_root("main.skills.../x")


@pytest.mark.parametrize("rel", ["", "/etc/passwd", "../x", "a/../../x", "a//b", "a\\b", "./a"])
def test_unsafe_relative_paths_are_refused(rel: str) -> None:
    with pytest.raises(SkillBundleError):
        safe_relative_path(rel)
