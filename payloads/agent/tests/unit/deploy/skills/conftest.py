"""Skills brick test fixtures."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_foundry.contracts.skills import SKILL_FILE, render_skill_md
from payloads.agent.deploy.skills.assemble import assemble_skills


def _write_eval_questions(skill_dir: Path, count: int, tool: str) -> None:
    eval_dir = skill_dir / "eval"
    eval_dir.mkdir(exist_ok=True)
    questions = "questions:\n"
    for i in range(count):
        questions += (
            f'  - input: "Test question {i}"\n'
            f"    expected_tools:\n"
            f"      - {tool}\n"
            f"    expected_output_contains:\n"
            f'      - "result"\n'
        )
    (eval_dir / "questions.yml").write_text(questions, encoding="utf-8")


def create_skill(
    parent_dir: Path,
    name: str,
    *,
    body: str = "1. Call test_tool\n2. Return result",
    eval_questions: int = 3,
) -> Path:
    """Create a valid ``<name>/SKILL.md`` skill with eval questions."""
    skill_dir = parent_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / SKILL_FILE).write_text(render_skill_md(name, f"Test skill: {name}", body), encoding="utf-8")
    _write_eval_questions(skill_dir, eval_questions, "test_tool")
    return skill_dir


def create_legacy_skill(
    parent_dir: Path,
    name: str,
    *,
    phase: int = 1,
    triggers: list[str] | None = None,
    tools: list[str] | None = None,
    prompt_sections: bool = True,
    eval_questions: int = 3,
) -> Path:
    """Create a deprecated ``skill.yml`` / ``prompt.md`` / ``tools.yml`` skill.

    *parent_dir* can be either a skills root (flat layout) or a domain
    directory (nested layout).
    """
    skill_dir = parent_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)

    triggers = triggers or [f"trigger for {name}"]
    tools = tools or ["test_tool"]

    (skill_dir / "skill.yml").write_text(
        f"name: {name}\n"
        f'description: "Test skill: {name}"\n'
        f"triggers:\n" + "".join(f'  - "{t}"\n' for t in triggers) + f"phase: {phase}\n"
        f"tools:\n" + "".join(f"  - {t}\n" for t in tools),
        encoding="utf-8",
    )

    if prompt_sections:
        prompt = (
            f"# {name}\n\n"
            "## When to activate\n\nActivate for test.\n\n"
            "## Prerequisites\n\nNone.\n\n"
            "## Workflow\n\n1. Call test_tool\n2. Return result\n\n"
            '## Tool call examples\n\n```\ntest_tool(param="val")\n```\n\n'
            "## Output format\n\nStructured summary.\n\n"
            "## Edge cases\n\n- No data: inform user\n\n"
            "## Rules\n\n- Always be helpful\n"
        )
    else:
        prompt = f"# {name}\n\nMinimal prompt without required sections.\n"

    (skill_dir / "prompt.md").write_text(prompt, encoding="utf-8")

    tools_yml = "tools:\n"
    for t in tools:
        tools_yml += (
            f"  - name: {t}\n"
            f"    type: uc_function\n"
            f'    function_name: "test_catalog.test_schema.{t}"\n'
            f'    description: "Test tool {t}"\n'
        )
    (skill_dir / "tools.yml").write_text(tools_yml, encoding="utf-8")

    _write_eval_questions(skill_dir, eval_questions, tools[0])
    return skill_dir


def create_domain(
    skills_dir: Path,
    name: str,
    *,
    description: str = "",
    business_process: str = "",
    domain_prompt: str = "",
    triggers: list[str] | None = None,
) -> Path:
    """Create a deprecated skill domain directory with ``domain.yml`` and optional ``domain_prompt.md``."""
    domain_dir = skills_dir / name
    domain_dir.mkdir(parents=True, exist_ok=True)

    desc = description or f"Test domain: {name}"
    bp = business_process or f"Business process for {name}."
    trigs = triggers or [f"trigger for domain {name}"]

    (domain_dir / "domain.yml").write_text(
        f"name: {name}\n"
        f'description: "{desc}"\n'
        f"triggers:\n" + "".join(f'  - "{t}"\n' for t in trigs) + f'business_process: "{bp}"\n',
        encoding="utf-8",
    )

    if domain_prompt:
        (domain_dir / "domain_prompt.md").write_text(domain_prompt, encoding="utf-8")

    return domain_dir


def _write_agent_payload(instance_dir: Path, system_prompt: str) -> None:
    """Write the agent payload manifest with a system prompt.

    Skills + the agent's system prompt are sourced from the payload's
    ``spec`` in ``payloads/agent/payload.yml`` (the single config
    contract); the assembler reads them from there.
    """
    import yaml

    payload_dir = instance_dir / "payloads" / "agent"
    payload_dir.mkdir(parents=True, exist_ok=True)
    (payload_dir / "payload.yml").write_text(
        yaml.dump({"payload": {"adapter": "agent", "spec": {"runtime": {"system_prompt": system_prompt}}}}),
        encoding="utf-8",
    )


def author_skill_spec(
    instance_dir: Path,
    *,
    loading: str | None = None,
    routing_header: str | None = None,
    prompt_cache_enabled: bool | None = None,
) -> None:
    """Merge ``spec.runtime.tools.skills`` and ``context.prompt_cache.enabled`` into ``payload.yml``."""
    import yaml

    path = instance_dir / "payloads" / "agent" / "payload.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else None
    doc = doc or {"payload": {"adapter": "agent", "spec": {}}}
    runtime = doc.setdefault("payload", {}).setdefault("spec", {}).setdefault("runtime", {})
    skills = runtime.setdefault("tools", {}).setdefault("skills", {})
    if loading is not None:
        skills["loading"] = loading
    if routing_header is not None:
        skills["routing_header"] = routing_header
    if prompt_cache_enabled is not None:
        runtime.setdefault("context", {}).setdefault("prompt_cache", {})["enabled"] = prompt_cache_enabled
    path.write_text(yaml.dump(doc), encoding="utf-8")


def set_lazy_loading(instance_dir: Path) -> None:
    """Lazy only survives assembly with prompt caching off, so author the pair."""
    author_skill_spec(instance_dir, loading="lazy", prompt_cache_enabled=False)


def _create_instance(tmp_path: Path, name: str, system_prompt: str) -> tuple[Path, Path]:
    """An instance with an agent payload; returns the instance and its ``skills/`` directory."""
    inst = tmp_path / "instances" / name
    _write_agent_payload(inst, system_prompt)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "test"\n')
    return inst, inst / "payloads" / "agent" / "skills"


@pytest.fixture
def instance_dir(tmp_path: Path) -> Path:
    """Create a minimal instance with two ``SKILL.md`` skills."""
    inst, skills_dir = _create_instance(tmp_path, "test_app", "You are a test assistant.")
    create_skill(skills_dir, "beta-skill")
    create_skill(skills_dir, "alpha-skill")
    return inst


@pytest.fixture
def legacy_instance_dir(tmp_path: Path) -> Path:
    """Create a minimal instance with one deprecated domain containing two legacy skills."""
    inst, skills_dir = _create_instance(tmp_path, "legacy_app", "You are a test assistant.")
    domain_dir = create_domain(
        skills_dir,
        "test_domain",
        domain_prompt="Test entities and relationships.",
    )
    create_legacy_skill(domain_dir, "alpha_skill", phase=2, tools=["tool_a", "tool_b"])
    create_legacy_skill(domain_dir, "beta_skill", phase=1, tools=["tool_b", "tool_c"])
    return inst


@pytest.fixture
def legacy_flat_instance_dir(tmp_path: Path) -> Path:
    """Create a minimal instance with flat (non-domain) legacy skills."""
    inst, skills_dir = _create_instance(tmp_path, "flat_app", "You are a flat test assistant.")
    create_legacy_skill(skills_dir, "alpha_skill", phase=2, tools=["tool_a", "tool_b"])
    create_legacy_skill(skills_dir, "beta_skill", phase=1, tools=["tool_b", "tool_c"])
    return inst


@pytest.fixture
def assembled(instance_dir: Path) -> dict[str, Any]:
    """Assemble all skills from the test instance."""
    return assemble_skills(instance_dir)


@pytest.fixture
def legacy_assembled(legacy_instance_dir: Path) -> dict[str, Any]:
    """Assemble all skills from the legacy domain instance."""
    return assemble_skills(legacy_instance_dir)
