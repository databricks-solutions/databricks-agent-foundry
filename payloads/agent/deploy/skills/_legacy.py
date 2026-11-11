"""The deprecated repo-skill format, read for one more release (#652).

``skill.yml`` + ``prompt.md`` + ``tools.yml`` per skill, optionally grouped
under a ``domain.yml``.  Legacy skills load into the same
:class:`~payloads.agent.deploy.skills.common.RepoSkill` as ``SKILL.md``
skills; only their ``tools.yml`` binding and its validation live here.
Every legacy file is reported as deprecated.  This module is deleted with
the format.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError as PydanticValidationError

from agent_foundry._instance_layout import resolve_payload_subdir
from agent_foundry.config import parse_yaml_file
from agent_foundry.contracts import (
    GenieTool,
    MCPTool,
    SkillDomainMetadata,
    SkillMetadata,
    Tool,
    ToolManifest,
    VectorSearchTool,
)
from agent_foundry.contracts.skills import SKILL_FILE, SkillDocument
from agent_foundry.variables import substitute_obj
from payloads.agent.deploy.skills.common import (
    RepoSkill,
    ValidationError,
    check_line_count,
    child_dirs,
    load_eval_questions,
)

SKILL_YML = "skill.yml"
PROMPT_MD = "prompt.md"
TOOLS_YML = "tools.yml"
DOMAIN_YML = "domain.yml"
DOMAIN_PROMPT_MD = "domain_prompt.md"
LEGACY_FILES: tuple[str, ...] = (SKILL_YML, PROMPT_MD, TOOLS_YML, DOMAIN_YML, DOMAIN_PROMPT_MD)

_REQUIRED_PROMPT_SECTIONS_TOOL_OWNING: tuple[str, ...] = (
    "workflow",
    "tool call examples",
    "output format",
    "edge cases",
)
_TYPED_TRANSPORT_TYPES: frozenset[str] = frozenset({"genie", "vector_search", "uc_function", "python"})
_TYPE_REQUIRED_FIELDS: dict[str, list[str]] = {
    "uc_function": ["function_name"],
    "python": ["callable_path"],
}
# MCP references are wired by the MCP brick; SKILL_TOOLS keeps only tools
# the agent runtime invokes directly.
_RUNTIME_IGNORED_TOOL_TYPES: tuple[type[Tool], ...] = (MCPTool,)

_GENIE_PARENT = "genie_spaces"
_VS_PARENT = "vector_indexes"
_GENIE_CONFIG = "space.yml"
_VS_CONFIG = "index_config.yml"


def is_legacy_skill(directory: Path) -> bool:
    return (directory / SKILL_YML).exists()


def is_legacy_domain(directory: Path) -> bool:
    return (directory / DOMAIN_YML).exists()


def deprecation_warning(path: str) -> ValidationError:
    return ValidationError(
        path,
        f"deprecated: move the skill to {SKILL_FILE} (frontmatter name and description, then the body); "
        "skill.yml, prompt.md, tools.yml and domain.yml are removed in the next release",
        severity="warning",
    )


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _validated(model: type[SkillMetadata] | type[SkillDomainMetadata], path: Path, label: str) -> Any:
    """*path* validated against *model*; ``None`` when it has no ``name`` (silently skipped)."""
    raw = parse_yaml_file(path)
    if not raw.get("name"):
        return None
    try:
        return model.model_validate(raw)
    except PydanticValidationError as exc:
        raise ValueError(f"{path}: invalid {label} metadata\n{exc}") from exc


def _normalize_tool_manifest(raw: dict[str, Any]) -> dict[str, Any]:
    """Tag ``{name, description}`` entries as ``type: mcp`` so the discriminated union resolves."""
    raw_tools = raw.get("tools", []) if isinstance(raw, dict) else []
    if not isinstance(raw_tools, list):
        return raw
    normalized = [
        {**entry, "type": "mcp"} if isinstance(entry, dict) and "type" not in entry else entry for entry in raw_tools
    ]
    return {**raw, "tools": normalized}


def _load_tool_manifest(path: Path) -> ToolManifest:
    raw = _normalize_tool_manifest(parse_yaml_file(path))
    try:
        return ToolManifest.model_validate(raw)
    except PydanticValidationError as exc:
        raise ValueError(f"{path}: invalid tool manifest\n{exc}") from exc


def load_legacy_skill(skill_dir: Path) -> RepoSkill | None:
    metadata: SkillMetadata | None = _validated(SkillMetadata, skill_dir / SKILL_YML, "skill")
    if metadata is None:
        return None
    body_path = skill_dir / PROMPT_MD
    body = body_path.read_text(encoding="utf-8").strip() if body_path.exists() else ""
    return RepoSkill(
        skill_id=skill_dir.name,
        document=SkillDocument(name=metadata.name, description=metadata.description.strip(), body=body),
        body_path=body_path,
        eval_questions=load_eval_questions(skill_dir),
        legacy_tools=_load_tool_manifest(skill_dir / TOOLS_YML),
    )


def load_legacy_domain(domain_dir: Path) -> tuple[str, list[RepoSkill]] | None:
    """The domain's name and its skills, flattened; ``domain_prompt.md`` is not read."""
    metadata: SkillDomainMetadata | None = _validated(SkillDomainMetadata, domain_dir / DOMAIN_YML, "domain")
    if metadata is None:
        return None
    skills = [skill for child in child_dirs(domain_dir) if (skill := load_legacy_skill(child)) is not None]
    return metadata.name, skills


# ---------------------------------------------------------------------------
# tools.yml -> SKILL_TOOLS
# ---------------------------------------------------------------------------


def _merge_tool_manifests(skills: list[RepoSkill]) -> tuple[list[Tool], dict[str, list[str]]]:
    """Tools of all skills, first occurrence of a name wins; ``used_by`` maps name to skill names."""
    seen: dict[str, Tool] = {}
    used_by: dict[str, list[str]] = {}
    for skill in skills:
        if skill.legacy_tools is None:
            continue
        for tool in skill.legacy_tools.tools:
            if tool.name not in seen:
                seen[tool.name] = tool
            used_by.setdefault(tool.name, []).append(skill.document.name)
    return list(seen.values()), used_by


def _scan_sub_instance_tool_names(parent: Path, config_filename: str) -> dict[str, str]:
    """``tool_name`` (default: the directory name) -> sub-instance directory name."""
    if not parent.is_dir():
        return {}
    mapping: dict[str, str] = {}
    for sub in sorted(parent.iterdir()):
        if not sub.is_dir() or sub.name.startswith(("_", ".")):
            continue
        tool_name = parse_yaml_file(sub / config_filename).get("tool_name") or sub.name
        if tool_name in mapping:
            raise ValueError(
                f"Duplicate tool_name '{tool_name}' under {parent}: declared "
                f"by both '{mapping[tool_name]}' and '{sub.name}'. "
                f"Add an explicit `tool_name:` field to one of their "
                f"{config_filename} files to disambiguate."
            )
        mapping[tool_name] = sub.name
    return mapping


def _resolve_skill_tool_refs(tools: list[Tool], instance_dir: Path, variables: dict[str, str]) -> None:
    """Fill unset genie / vector search ids from the sub-instance whose ``tool_name`` matches; mutates *tools*."""
    genie_map = _scan_sub_instance_tool_names(instance_dir / _GENIE_PARENT, _GENIE_CONFIG)
    vs_map = _scan_sub_instance_tool_names(instance_dir / _VS_PARENT, _VS_CONFIG)
    for tool in tools:
        if isinstance(tool, GenieTool) and not tool.space_id:
            sub = genie_map.get(tool.name)
            if sub:
                tool.space_id = variables.get(f"genie_space_id__{sub}", "")
        elif isinstance(tool, VectorSearchTool) and not tool.index_name:
            sub = vs_map.get(tool.name)
            if sub:
                tool.index_name = variables.get(f"vs_index_name__{sub}", "")
                if not tool.endpoint_name:
                    tool.endpoint_name = variables.get(f"vs_endpoint_name__{sub}", "")


def _dump_tools(tools: list[Tool], used_by: dict[str, list[str]]) -> list[dict[str, Any]]:
    """Direct-mode tools as dicts; ``exclude_unset`` keeps omitted defaults absent."""
    result: list[dict[str, Any]] = []
    for tool in tools:
        if isinstance(tool, _RUNTIME_IGNORED_TOOL_TYPES):
            continue
        entry = tool.model_dump(exclude_unset=True)
        entry["used_by"] = used_by[tool.name]
        result.append(entry)
    return result


def legacy_skill_tools(
    skills: list[RepoSkill],
    instance_dir: Path,
    variables: dict[str, str] | None,
) -> list[dict[str, Any]]:
    """``SKILL_TOOLS`` from the skills' ``tools.yml``; empty when no skill has one."""
    tools, used_by = _merge_tool_manifests(skills)
    if variables:
        _resolve_skill_tool_refs(tools, instance_dir, variables)
    dumped = _dump_tools(tools, used_by)
    return substitute_obj(dumped, variables, strict=False) if variables else dumped


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _read_yaml_checked(path: Path, prefix: str, errors: list[ValidationError]) -> dict[str, Any]:
    if not path.exists():
        errors.append(ValidationError(prefix, "file is missing"))
        return {}
    if not path.read_text(encoding="utf-8").strip():
        errors.append(ValidationError(prefix, "file is empty"))
        return {}
    return parse_yaml_file(path)


def _check_non_empty_strings(
    data: dict[str, Any], fields: tuple[str, ...], prefix: str, errors: list[ValidationError]
) -> None:
    for field in fields:
        if field not in data:
            errors.append(ValidationError(prefix, f"required field '{field}' is missing"))
        elif not isinstance(data[field], str) or not data[field].strip():
            errors.append(ValidationError(prefix, f"'{field}' must be a non-empty string"))


def _validate_skill_yml(skill_dir: Path, prefix: str, errors: list[ValidationError]) -> dict[str, Any]:
    yml_prefix = f"{prefix}/{SKILL_YML}"
    data = _read_yaml_checked(skill_dir / SKILL_YML, yml_prefix, errors)
    if not data:
        return data
    _check_non_empty_strings(data, ("name", "description"), yml_prefix, errors)
    if "triggers" not in data:
        errors.append(ValidationError(yml_prefix, "required field 'triggers' is missing"))
    elif not isinstance(data["triggers"], list) or not data["triggers"]:
        errors.append(ValidationError(yml_prefix, "'triggers' must be a non-empty list"))
    if "tools" not in data:
        errors.append(ValidationError(yml_prefix, "required field 'tools' is missing"))
    elif not isinstance(data["tools"], list):
        errors.append(ValidationError(yml_prefix, "'tools' must be a list"))
    return data


def _is_tool_less(skill_data: dict[str, Any]) -> bool:
    """An explicit ``tools: []``: no ``tools.yml`` and no mandatory headings needed."""
    tools = skill_data.get("tools")
    return isinstance(tools, list) and not tools


def _validate_prompt_md(skill_dir: Path, prefix: str, errors: list[ValidationError], *, is_tool_less: bool) -> None:
    path = skill_dir / PROMPT_MD
    md_prefix = f"{prefix}/{PROMPT_MD}"
    if not path.exists():
        errors.append(ValidationError(md_prefix, "file is missing"))
        return
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        errors.append(ValidationError(md_prefix, "file is empty"))
        return
    check_line_count(text, md_prefix, errors)
    if is_tool_less:
        return
    headings = [line.lstrip("#").strip().lower() for line in text.split("\n") if line.startswith("#")]
    for section in _REQUIRED_PROMPT_SECTIONS_TOOL_OWNING:
        if not any(section in h for h in headings):
            errors.append(ValidationError(md_prefix, f"missing required section: '{section}'"))


def _check_mcp_brick_reference(
    tool_prefix: str,
    name: str,
    mcp_brick_tools: set[str] | None,
    errors: list[ValidationError],
) -> None:
    """Unresolved MCP references are errors; skipped without instance context or a name."""
    if mcp_brick_tools is None or not name or name in mcp_brick_tools:
        return
    errors.append(
        ValidationError(
            tool_prefix,
            (
                f"MCP tool reference {name!r} does not match any "
                "`tools.allow` entry under "
                "instances/<app>/mcp_servers/*/server.yml. "
                "Did you forget to register the MCP server, or misspell "
                "the tool name? Run "
                "`agent-foundry brick mcp inspect <server> --instance <app>` "
                "to list the actual tool names the server exposes."
            ),
        )
    )


def _validate_tool_entry(
    tool: dict[str, Any],
    tool_prefix: str,
    mcp_brick_tools: set[str] | None,
    errors: list[ValidationError],
) -> None:
    for field in ("name", "description"):
        if field not in tool:
            errors.append(ValidationError(tool_prefix, f"required field '{field}' is missing"))
    name = tool.get("name", "")
    tool_type = tool.get("type", "")
    if not tool_type or tool_type == "mcp":
        _check_mcp_brick_reference(tool_prefix, name, mcp_brick_tools, errors)
        return
    if tool_type not in _TYPED_TRANSPORT_TYPES:
        valid = sorted(_TYPED_TRANSPORT_TYPES | {"mcp"})
        errors.append(
            ValidationError(
                tool_prefix,
                f"unknown tool type '{tool_type}' (expected: {', '.join(valid)}, or omit `type:` for an MCP reference)",
                severity="warning",
            )
        )
        return
    for req_field in _TYPE_REQUIRED_FIELDS.get(tool_type, []):
        if req_field not in tool:
            errors.append(ValidationError(tool_prefix, f"type '{tool_type}' requires field '{req_field}'"))


def _validate_tools_yml(
    skill_dir: Path,
    prefix: str,
    errors: list[ValidationError],
    *,
    mcp_brick_tools: set[str] | None,
    is_tool_less: bool,
) -> set[str]:
    """Check each ``tools.yml`` entry; returns the declared tool names."""
    path = skill_dir / TOOLS_YML
    tools_prefix = f"{prefix}/{TOOLS_YML}"
    if not path.exists():
        if not is_tool_less:
            errors.append(ValidationError(tools_prefix, "file is missing"))
        return set()
    if not path.read_text(encoding="utf-8").strip():
        errors.append(ValidationError(tools_prefix, "file is empty"))
        return set()
    tools = parse_yaml_file(path).get("tools", [])
    if not isinstance(tools, list):
        errors.append(ValidationError(tools_prefix, "'tools' must be a list"))
        return set()
    for i, tool in enumerate(tools):
        _validate_tool_entry(tool, f"{tools_prefix}[{i}]", mcp_brick_tools, errors)
    return {tool["name"] for tool in tools if tool.get("name")}


def _validate_tool_cross_refs(
    skill_data: dict[str, Any],
    prefix: str,
    tools_yml_names: set[str],
    errors: list[ValidationError],
) -> None:
    skill_tools = skill_data.get("tools", [])
    if not isinstance(skill_tools, list) or not tools_yml_names:
        return
    for tool_name in skill_tools:
        if tool_name not in tools_yml_names:
            errors.append(
                ValidationError(
                    f"{prefix}/{SKILL_YML}",
                    f"tool '{tool_name}' listed in skill.yml but not defined in tools.yml",
                    severity="warning",
                )
            )


def validate_legacy_skill_dir(
    skill_dir: Path,
    prefix: str,
    *,
    mcp_brick_tools: set[str] | None = None,
) -> list[ValidationError]:
    """The old ``skill.yml`` / ``prompt.md`` / ``tools.yml`` rules; eval questions are checked by the caller."""
    errors: list[ValidationError] = [deprecation_warning(f"{prefix}/{SKILL_YML}")]
    skill_data = _validate_skill_yml(skill_dir, prefix, errors)
    is_tool_less = _is_tool_less(skill_data)
    _validate_prompt_md(skill_dir, prefix, errors, is_tool_less=is_tool_less)
    tool_names = _validate_tools_yml(
        skill_dir,
        prefix,
        errors,
        mcp_brick_tools=mcp_brick_tools,
        is_tool_less=is_tool_less,
    )
    _validate_tool_cross_refs(skill_data, prefix, tool_names, errors)
    return errors


def validate_legacy_domain_yml(domain_dir: Path) -> list[ValidationError]:
    """``domain.yml`` itself; its skills are validated by the caller like flat skills."""
    domain_name = domain_dir.name
    prefix = f"{domain_name}/{DOMAIN_YML}"
    errors: list[ValidationError] = [deprecation_warning(prefix)]
    data = _read_yaml_checked(domain_dir / DOMAIN_YML, prefix, errors)
    if data:
        _check_non_empty_strings(data, ("name", "description"), prefix, errors)
    if (domain_dir / DOMAIN_PROMPT_MD).exists():
        errors.append(
            ValidationError(
                f"{domain_name}/{DOMAIN_PROMPT_MD}",
                "no longer added to the prompt: move shared context into spec.runtime.system_prompt",
                severity="warning",
            )
        )
    if not any(is_legacy_skill(child) for child in child_dirs(domain_dir)):
        errors.append(
            ValidationError(f"{domain_name}/", "skill domain has no skill subdirectories", severity="warning")
        )
    return errors


def legacy_domain_skill_dirs(domain_dir: Path) -> list[Path]:
    return [child for child in child_dirs(domain_dir) if is_legacy_skill(child)]


def _allow_entry_alias(item: Any) -> str | None:
    """The LLM-visible name of one ``tools.allow`` entry; globs without ``expose_as`` are skipped."""
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        expose_as = item.get("expose_as")
        if isinstance(expose_as, str) and expose_as:
            return expose_as
        match = item.get("match")
        if isinstance(match, str) and match and not any(ch in match for ch in "*?["):
            return match
    return None


def collect_mcp_brick_tool_names(instance_dir: Path) -> set[str]:
    """Union of every ``mcp_servers/*/server.yml`` ``tools.allow`` alias; malformed files are skipped."""
    names: set[str] = set()
    for sub in child_dirs(resolve_payload_subdir(instance_dir, "mcp_servers")):
        server_yml = sub / "server.yml"
        if sub.name.startswith(".") or not server_yml.exists():
            continue
        try:
            data = yaml.safe_load(server_yml.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            continue
        allow = (data.get("tools") or {}).get("allow") or []
        if not isinstance(allow, list):
            continue
        names.update(alias for item in allow if (alias := _allow_entry_alias(item)) is not None)
    return names
