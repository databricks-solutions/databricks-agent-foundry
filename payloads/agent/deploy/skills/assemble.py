"""
Skills Assembler
================
Reads an instance's repo skills -- one ``skills/<skill_id>/SKILL.md`` each
(frontmatter ``name`` and ``description``, then the body) -- and composes
the system prompt.

Usage:
    agent-foundry brick skills assemble --instance <name> [--target <dev|prod>] [--vars key=val ...]
    python -m payloads.agent.deploy.skills.assemble --instance <name>

Output (eager mode, default):
    instances/<app>/skills/_assembled/system_prompt.md
    instances/<app>/skills/_assembled/skill_tools.json

Lazy mode (``spec.runtime.tools.skills.loading: lazy`` in ``payload.yml``, issue #98):
    Same as eager plus ``_assembled/skill_index.json``.  The system prompt
    carries only the skill index; ``_load_skill`` reads bodies on demand.

The deprecated ``skill.yml`` / ``prompt.md`` / ``tools.yml`` / ``domain.yml``
format is still read for one release (see :mod:`._legacy`); its
``tools.yml`` entries are the only source of ``skill_tools.json`` tools.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from agent_foundry._instance_layout import resolve_payload_subdir
from agent_foundry.bricks import load_brick_variables
from agent_foundry.contracts.skills import SKILL_FILE, SkillDocumentError, parse_skill_md
from agent_foundry.variables import add_project_root_arg
from payloads.agent.config import load_agent_spec
from payloads.agent.contracts import DEFAULT_SKILL_ROUTING_HEADER, SkillLoadingMode, effective_skill_loading
from payloads.agent.deploy.skills import _legacy
from payloads.agent.deploy.skills.common import RepoSkill, child_dirs, load_eval_questions
from payloads.agent.runtime.gateway_skills import render_skill_index

# ``eager`` inlines every skill body into the system prompt; ``lazy`` emits
# only an index plus ``_assembled/skill_index.json`` for ``_load_skill``.
_SECTION_SEPARATOR = "\n\n---\n\n"


def _read_skill_loading_mode(instance_dir: Path) -> SkillLoadingMode:
    """``spec.runtime.tools.skills.loading``, defaulting to eager; prompt caching overrides lazy."""
    spec = load_agent_spec(instance_dir)
    return resolve_skill_loading_mode(
        spec.runtime.tools.skills.loading,
        prompt_cache_enabled=bool(spec.runtime.context.prompt_cache.enabled),
    )


def resolve_skill_loading_mode(
    authored: SkillLoadingMode | None,
    *,
    prompt_cache_enabled: bool,
) -> SkillLoadingMode:
    """Return the mode to deploy with, overriding lazy when caching is on.

    Lazy loading is structurally cache-hostile.  Skill bodies arrive as
    *tool results* in the conversation, behind the volatile tail, so they
    are re-sent and re-written at full price on every request instead of
    riding the cached prefix -- and each activation costs an extra LLM
    round trip on top.  Measured on a multi-skill question, lazy cost 3x
    what eager cost with caching active.

    Caching therefore wins rather than the two being allowed to coexist:
    the authored intent behind ``lazy`` (a smaller prompt) is better served
    by a cached large prompt than by an uncached small one.
    """
    mode = effective_skill_loading(authored, prompt_cache_enabled=prompt_cache_enabled)
    if authored != "lazy" or mode == "lazy":
        return mode
    print(
        "  WARNING: skills.loading 'lazy' is ignored while context.prompt_cache is enabled "
        "-- deploying eager. Lazy skill bodies arrive as tool results behind the cached prefix, "
        "so they are re-written at full price on every request (measured: 3x the cost of eager). "
        "Set context.prompt_cache.enabled: false to keep lazy loading."
    )
    return mode


def _read_skill_routing_header(instance_dir: Path) -> str:
    """``spec.runtime.tools.skills.routing_header`` when non-empty, else the default; lazy mode only."""
    header = load_agent_spec(instance_dir).runtime.tools.skills.routing_header
    if header and header.strip():
        return header
    return DEFAULT_SKILL_ROUTING_HEADER


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def _load_skill_md(skill_dir: Path) -> RepoSkill:
    path = skill_dir / SKILL_FILE
    try:
        document = parse_skill_md(path.read_text(encoding="utf-8"))
    except SkillDocumentError as exc:
        raise ValueError(f"{path}: {exc}") from exc
    return RepoSkill(
        skill_id=skill_dir.name,
        document=document,
        body_path=path,
        eval_questions=load_eval_questions(skill_dir),
    )


def load_skill(skill_dir: Path) -> RepoSkill | None:
    """The skill in *skill_dir*: ``SKILL.md`` first, else the legacy format, else ``None``."""
    if (skill_dir / SKILL_FILE).is_file():
        return _load_skill_md(skill_dir)
    if _legacy.is_legacy_skill(skill_dir):
        return _legacy.load_legacy_skill(skill_dir)
    return None


def discover_skills(instance_dir: Path) -> tuple[list[RepoSkill], list[str]]:
    """All skills, sorted by ``skill_id``, and the names of legacy domains they were flattened from."""
    skills: list[RepoSkill] = []
    domain_names: list[str] = []
    for directory in child_dirs(resolve_payload_subdir(instance_dir, "skills")):
        if _legacy.is_legacy_domain(directory):
            domain = _legacy.load_legacy_domain(directory)
            if domain is not None:
                domain_names.append(domain[0])
                skills.extend(domain[1])
        elif (skill := load_skill(directory)) is not None:
            skills.append(skill)
    return sorted(skills, key=lambda s: s.skill_id), domain_names


# ---------------------------------------------------------------------------
# Prompt composition
# ---------------------------------------------------------------------------


def _skill_section(skill: RepoSkill) -> str:
    parts = [f"### Skill: {skill.document.name}"]
    parts.extend(text for text in (skill.document.description, skill.document.body) if text)
    return "\n\n".join(parts)


def compose_system_prompt(agent_prompt: str, skills: list[RepoSkill]) -> str:
    """Eager: the agent prompt, then every skill's name, description and body."""
    sections = [agent_prompt] if agent_prompt else []
    if skills:
        sections.append("## Skills\n\n" + "\n\n".join(_skill_section(skill) for skill in skills))
    return _SECTION_SEPARATOR.join(sections)


def compose_routing_index_prompt(
    agent_prompt: str,
    skills: list[RepoSkill],
    routing_header: str = DEFAULT_SKILL_ROUTING_HEADER,
) -> str:
    """Lazy: the agent prompt, then the same index user and gateway skills use."""
    rows = [(skill.skill_id, skill.document.name, skill.document.description) for skill in skills]
    sections = [text for text in (agent_prompt, render_skill_index(rows, header=routing_header)) if text]
    return _SECTION_SEPARATOR.join(sections)


def build_skill_index(instance_dir: Path, skills: list[RepoSkill]) -> dict[str, str]:
    """``skill_id`` -> body file, relative to ``skills/`` so the artefact stays portable."""
    skills_root = resolve_payload_subdir(instance_dir, "skills")
    return {skill.skill_id: skill.body_path.relative_to(skills_root).as_posix() for skill in skills}


def aggregate_eval_questions(skills: list[RepoSkill]) -> list[dict[str, Any]]:
    """Eval questions of all skills, each tagged with its skill's name."""
    return [{**question, "skill": skill.document.name} for skill in skills for question in skill.eval_questions]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def assemble_skills(
    instance_dir: Path,
    variables: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Assemble an instance's skills.

    Returns a dict with:
        - system_prompt: str -- full bodies (eager) or the skill index (lazy)
        - tools: list -- ``SKILL_TOOLS`` from legacy ``tools.yml`` files
        - eval_questions: list -- aggregated eval questions
        - skill_names: list[str] -- names of all assembled skills
        - domain_names: list[str] -- legacy domains the skills were flattened from
        - skill_loading: ``"eager"`` | ``"lazy"``
        - skill_index: ``dict[skill_id, relative_body_path]`` in lazy mode, else ``None``
    """
    skills, domain_names = discover_skills(instance_dir)
    agent_prompt = load_agent_spec(instance_dir).runtime.system_prompt or ""

    mode = _read_skill_loading_mode(instance_dir)
    if mode == "lazy":
        system_prompt = compose_routing_index_prompt(agent_prompt, skills, _read_skill_routing_header(instance_dir))
        skill_index: dict[str, str] | None = build_skill_index(instance_dir, skills)
    else:
        system_prompt = compose_system_prompt(agent_prompt, skills)
        skill_index = None

    return {
        "system_prompt": system_prompt,
        "tools": _legacy.legacy_skill_tools(skills, instance_dir, variables),
        "eval_questions": aggregate_eval_questions(skills),
        "skill_names": [skill.document.name for skill in skills],
        "domain_names": domain_names,
        "skill_loading": mode,
        "skill_index": skill_index,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Assemble skills into a system prompt and tool manifest",
    )
    parser.add_argument("--instance", required=True, help="Instance name under instances/")
    parser.add_argument("--target", default="dev", help="Deployment target (dev, staging, prod)")
    parser.add_argument("--vars", nargs="*", default=[], help="Extra variables as key=value pairs")
    parser.add_argument(
        "--no-resolve",
        action="store_true",
        help="Skip variable resolution (keep ${var.xxx} references)",
    )
    add_project_root_arg(parser)
    args = parser.parse_args(argv)

    project_root = args.project_root
    instance_dir = project_root / "instances" / args.instance
    if not instance_dir.exists():
        print(f"Error: Instance directory not found: {instance_dir}", file=sys.stderr)
        return 1

    variables = (
        {} if args.no_resolve else load_brick_variables(project_root, args.target, instance_dir, extra_vars=args.vars)
    )

    result = assemble_skills(instance_dir, variables or None)

    out_dir = resolve_payload_subdir(instance_dir, "skills") / "_assembled"
    out_dir.mkdir(parents=True, exist_ok=True)

    prompt_path = out_dir / "system_prompt.md"
    prompt_path.write_text(result["system_prompt"], encoding="utf-8")

    n_skills = len(result["skill_names"])
    prompt_lines = result["system_prompt"].count("\n") + 1

    print(f"\n  Assembled: {args.instance} -> {prompt_path}")
    print(f"    Skills:        {n_skills} ({', '.join(result['skill_names'])})")
    print(f"    Tools:         {len(result['tools'])}")
    print(f"    Eval questions: {len(result['eval_questions'])}")
    print(f"    Prompt lines:  {prompt_lines}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
