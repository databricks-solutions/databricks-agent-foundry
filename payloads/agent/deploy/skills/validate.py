"""
Skills Validator
================
Validates an instance's repo skills without deploying.

A skill is ``skills/<skill_id>/SKILL.md``:
    - the directory name is a slug (lowercase letters, digits, single hyphens)
    - frontmatter ``name`` equals the directory name; ``description`` is
      non-empty and at most 1024 characters; the body is non-empty
    - the file is under 500 lines (warning)
    - ``eval/questions.yml`` exists with enough questions (warning)

Instance-level: ``spec.runtime.tools.skills.loading`` is valid and
``skill_id``s are unique.  The deprecated ``skill.yml`` format is still
validated by its old rules for one release (see :mod:`._legacy`), with a
deprecation warning.

Usage:
    agent-foundry brick skills validate --instance <name>
    agent-foundry brick skills validate --skill-dir <path>
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from agent_foundry._instance_layout import resolve_payload_subdir
from agent_foundry._payload_spec import read_spec, spec_section
from agent_foundry.contracts.skills import SKILL_FILE, SkillDocumentError, is_skill_slug, parse_skill_md
from agent_foundry.variables import add_project_root_arg
from payloads.agent.deploy.skills import _legacy
from payloads.agent.deploy.skills.common import ValidationError, check_eval, check_line_count, child_dirs

_VALID_SKILL_LOADING_VALUES: frozenset[str] = frozenset({"eager", "lazy"})


# ---------------------------------------------------------------------------
# Skill validation
# ---------------------------------------------------------------------------


def _validate_skill_md(skill_dir: Path, prefix: str, errors: list[ValidationError]) -> None:
    md_prefix = f"{prefix}/{SKILL_FILE}"
    if not is_skill_slug(skill_dir.name):
        errors.append(
            ValidationError(f"{prefix}/", "directory name must be lowercase letters, digits and single hyphens")
        )
    text = (skill_dir / SKILL_FILE).read_text(encoding="utf-8")
    try:
        document = parse_skill_md(text)
    except SkillDocumentError as exc:
        errors.append(ValidationError(md_prefix, str(exc)))
        return
    if document.name != skill_dir.name:
        errors.append(ValidationError(md_prefix, f"frontmatter name {document.name!r} must equal the directory name"))
    if not document.body:
        errors.append(ValidationError(md_prefix, "body is empty"))
    check_line_count(text, md_prefix, errors)
    for leftover in _legacy.LEGACY_FILES:
        if (skill_dir / leftover).exists():
            errors.append(
                ValidationError(f"{prefix}/{leftover}", f"ignored next to {SKILL_FILE}; delete it", severity="warning")
            )


def validate_skill_dir(
    skill_dir: Path,
    *,
    path_prefix: str | None = None,
    mcp_brick_tools: set[str] | None = None,
) -> list[ValidationError]:
    """Validate one skill directory; *mcp_brick_tools* only matters for legacy ``tools.yml``."""
    prefix = path_prefix or skill_dir.name
    if (skill_dir / SKILL_FILE).is_file():
        errors: list[ValidationError] = []
        _validate_skill_md(skill_dir, prefix, errors)
    elif _legacy.is_legacy_skill(skill_dir):
        errors = _legacy.validate_legacy_skill_dir(skill_dir, prefix, mcp_brick_tools=mcp_brick_tools)
    else:
        return [ValidationError(f"{prefix}/{SKILL_FILE}", "file is missing")]
    check_eval(skill_dir, prefix, errors)
    return errors


def validate_domain_dir(
    domain_dir: Path,
    *,
    mcp_brick_tools: set[str] | None = None,
) -> list[ValidationError]:
    """A legacy ``domain.yml`` directory and the skills nested in it."""
    errors = _legacy.validate_legacy_domain_yml(domain_dir)
    for skill_dir in _legacy.legacy_domain_skill_dirs(domain_dir):
        errors.extend(
            validate_skill_dir(
                skill_dir,
                path_prefix=f"{domain_dir.name}/{skill_dir.name}",
                mcp_brick_tools=mcp_brick_tools,
            )
        )
    return errors


# ---------------------------------------------------------------------------
# Instance-level validation
# ---------------------------------------------------------------------------


def _validate_skill_loading_field(
    instance_dir: Path,
    errors: list[ValidationError],
) -> None:
    """Reject skill-loading mode values outside the allowed set (issue #98)."""
    spec = read_spec(instance_dir)
    skills = spec_section(spec, "runtime", "tools", "skills")
    value = skills.get("loading")
    if value is not None and value not in _VALID_SKILL_LOADING_VALUES:
        errors.append(
            ValidationError(
                "payloads/agent/payload.yml",
                f"spec.runtime.tools.skills.loading must be 'eager' or 'lazy' (got {value!r})",
            )
        )
        return

    prompt_cache = spec_section(spec, "runtime", "context", "prompt_cache")
    if value == "lazy" and prompt_cache.get("enabled", True):
        errors.append(
            ValidationError(
                "payloads/agent/payload.yml",
                "spec.runtime.tools.skills.loading 'lazy' will be ignored: "
                "context.prompt_cache is enabled and the deploy switches to eager "
                "(lazy skill bodies are re-written at full price on every request). "
                "Author 'eager', or disable context.prompt_cache to keep lazy.",
                severity="warning",
            )
        )


def _is_skill_dir(directory: Path) -> bool:
    return (directory / SKILL_FILE).is_file() or _legacy.is_legacy_skill(directory)


def _gather_skill_id_paths(instance_dir: Path) -> dict[str, list[str]]:
    """``skill_id`` (the directory name, also for skills in legacy domains) -> paths relative to the instance."""
    seen: dict[str, list[str]] = {}
    for child in child_dirs(resolve_payload_subdir(instance_dir, "skills")):
        skill_dirs = _legacy.legacy_domain_skill_dirs(child) if _legacy.is_legacy_domain(child) else [child]
        for skill_dir in skill_dirs:
            if _is_skill_dir(skill_dir):
                seen.setdefault(skill_dir.name, []).append(str(skill_dir.relative_to(instance_dir)))
    return seen


def _emit_duplicate_skill_id_errors(
    seen: dict[str, list[str]],
    errors: list[ValidationError],
) -> None:
    """An error for every ``skill_id`` with more than one source path."""
    for skill_id, paths in seen.items():
        if len(paths) > 1:
            errors.append(
                ValidationError(
                    "skills/",
                    f"duplicate skill_id {skill_id!r}: {', '.join(paths)}",
                )
            )


def validate_instance_skills(instance_dir: Path) -> list[ValidationError]:
    """Validate every skill (and legacy domain) under an instance directory."""
    skills_root = resolve_payload_subdir(instance_dir, "skills")
    if not skills_root.is_dir():
        return []

    mcp_brick_tools = _legacy.collect_mcp_brick_tool_names(instance_dir)
    all_errors: list[ValidationError] = []
    _validate_skill_loading_field(instance_dir, all_errors)
    _emit_duplicate_skill_id_errors(_gather_skill_id_paths(instance_dir), all_errors)
    found_anything = False

    for child in child_dirs(skills_root):
        if _legacy.is_legacy_domain(child):
            found_anything = True
            all_errors.extend(validate_domain_dir(child, mcp_brick_tools=mcp_brick_tools))
        elif _is_skill_dir(child):
            found_anything = True
            all_errors.extend(validate_skill_dir(child, mcp_brick_tools=mcp_brick_tools))

    if not found_anything:
        all_errors.append(
            ValidationError(
                "skills/",
                f"no skill directories with a {SKILL_FILE} found",
                severity="warning",
            )
        )

    return all_errors


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate skill definitions")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--instance", help="Instance name under instances/")
    group.add_argument("--skill-dir", help="Path to a single skill directory")
    add_project_root_arg(parser)
    args = parser.parse_args(argv)

    if args.skill_dir:
        skill_dir = Path(args.skill_dir)
        if not skill_dir.is_dir():
            print(f"Error: not a directory: {skill_dir}", file=sys.stderr)
            return 1
        errors = validate_skill_dir(skill_dir)
    else:
        instance_dir = args.project_root / "instances" / args.instance
        if not instance_dir.exists():
            print(f"Error: instance directory not found: {instance_dir}", file=sys.stderr)
            return 1
        errors = validate_instance_skills(instance_dir)

    if not errors:
        print("Validation passed: no errors found.")
        return 0

    error_count = sum(1 for e in errors if e.severity == "error")
    warn_count = sum(1 for e in errors if e.severity != "error")

    for e in errors:
        print(str(e))

    print(f"\n{error_count} error(s), {warn_count} warning(s)")
    return 1 if error_count > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
