"""What the ``SKILL.md`` and the legacy repo-skill paths share.

A repo skill is one directory under ``payloads/agent/skills/``; its name is
the ``skill_id``.  Both formats load into :class:`RepoSkill`, so the
assembler renders and indexes them the same way.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_foundry.config import parse_yaml_file
from agent_foundry.contracts import ToolManifest
from agent_foundry.contracts.skills import SkillDocument

MAX_PROMPT_LINES = 500
MIN_EVAL_QUESTIONS = 3
EVAL_FILE = Path("eval") / "questions.yml"


@dataclass(frozen=True)
class RepoSkill:
    """One loaded repo skill; ``legacy_tools`` is only set for a ``tools.yml`` skill."""

    skill_id: str
    document: SkillDocument
    body_path: Path
    eval_questions: list[dict[str, Any]]
    legacy_tools: ToolManifest | None = None


class ValidationError:
    def __init__(self, path: str, message: str, severity: str = "error"):
        self.path = path
        self.message = message
        self.severity = severity

    def __str__(self) -> str:
        icon = "ERROR" if self.severity == "error" else "WARN"
        return f"[{icon}] {self.path}: {self.message}"


def load_eval_questions(skill_dir: Path) -> list[dict[str, Any]]:
    questions = parse_yaml_file(skill_dir / EVAL_FILE).get("questions", [])
    return questions if isinstance(questions, list) else []


def child_dirs(directory: Path) -> list[Path]:
    """Sub-directories of *directory*, sorted, without ``_``-prefixed ones."""
    if not directory.is_dir():
        return []
    return sorted(d for d in directory.iterdir() if d.is_dir() and not d.name.startswith("_"))


def check_line_count(text: str, path: str, errors: list[ValidationError]) -> None:
    line_count = len(text.split("\n"))
    if line_count > MAX_PROMPT_LINES:
        errors.append(
            ValidationError(
                path,
                f"prompt is {line_count} lines (recommended max: {MAX_PROMPT_LINES})",
                severity="warning",
            )
        )


def check_eval(skill_dir: Path, prefix: str, errors: list[ValidationError]) -> None:
    """Warn when ``eval/questions.yml`` is missing or has too few questions."""
    path = skill_dir / EVAL_FILE
    eval_prefix = f"{prefix}/{EVAL_FILE.as_posix()}"

    if not path.exists():
        errors.append(ValidationError(eval_prefix, "file is missing (recommended)", severity="warning"))
        return
    if not path.read_text(encoding="utf-8").strip():
        errors.append(ValidationError(eval_prefix, "file is empty", severity="warning"))
        return

    questions = load_eval_questions(skill_dir)
    if len(questions) < MIN_EVAL_QUESTIONS:
        errors.append(
            ValidationError(
                eval_prefix,
                f"only {len(questions)} question(s) (recommended min: {MIN_EVAL_QUESTIONS})",
                severity="warning",
            )
        )
