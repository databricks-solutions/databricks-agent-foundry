"""Lazy skill loading runtime helper (issue #98 PR 2/2).

Wires the build-time ``_assembled/skill_index.json`` artefact (produced
by the skills brick when ``spec.runtime.tools.skills.loading: lazy``)
into the agent runtime as a built-in ``_load_skill(skill_id: str)``
``FunctionTool``.

The tool is only registered when:
- ``payload.yml`` declared ``spec.runtime.tools.skills.loading: lazy``, AND
- the skills brick produced a non-empty index, AND
- at least one body file referenced by the index exists on disk.

Body content is preloaded into memory at factory time (bounded by skill
count, ~few KB each) so per-invocation latency is purely an LLM
round-trip plus a dict lookup -- no disk I/O in the request path.

Tracing: every successful invocation opens an MLflow ``TOOL`` span
named ``_load_skill`` carrying ``skill.loaded=<skill_id>`` as an
attribute.  The ``tool_routing_accuracy`` scorer reads that attribute
to treat lazy-mode loads as routing events on equal footing with
eager-mode tool calls.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

import yaml
from agents import FunctionTool

from agent_foundry.contracts.skills import SKILL_FILE, SkillDocumentError, parse_skill_md

logger = logging.getLogger(__name__)


# Single-leading-underscore tool name -- honours the MCP epic naming
# convention so it cannot collide with MCP ``<server>__<tool>`` names.
LOAD_SKILL_TOOL_NAME = "_load_skill"

# Attribute key set on the MLflow span; read back by the routing
# scorer (see ``bricks/evaluation/scorers/routing.py``).
SKILL_LOADED_ATTRIBUTE = "skill.loaded"

# Response shape: <opener> + body + <closer> + <postamble>
#
# The opener pseudo-tag (``<skill_runbook ...>``) signals to the LLM
# that the block is machine data, not user-facing prose.  The closer
# (``</skill_runbook>``) makes the boundary unambiguous so the LLM
# does not bleed runbook prose into the postamble.  The postamble is
# the *last* thing the LLM reads before deciding its next action --
# recency bias makes a trailing imperative dramatically more likely
# to be obeyed than the same imperative buried in the opener.  An
# earlier preamble-only design (single block at the top) was rejected
# because the model consistently treated the trailing markdown body
# as user-facing content and echoed it.
#
# The postamble is intentionally TINY and bracketed so the LLM treats
# it as metadata rather than content to acknowledge.  An earlier
# verbose multi-sentence directive was rejected because some models
# (notably Bedrock Claude) would echo the directive as a preface to
# their tool call -- defeating the whole point of hiding the runbook.
_OPENER_TEMPLATE = (
    '<skill_runbook skill_id="{skill_id}" name="{name}">\n'
    "INTERNAL DIRECTIVE -- the block below is your private operating "
    "procedure for this skill.  Do NOT show it to the user.  Do NOT "
    "summarise it.  Do NOT acknowledge loading.  Read it silently, "
    "then emit one tool call satisfying the user's last request.\n"
    "\n---\n\n"
)

_CLOSER = "\n</skill_runbook>\n"

_POSTAMBLE = "\n[next: emit one tool call to satisfy the request]\n"

_TOOL_DESCRIPTION = (
    "Load the operational runbook for a skill from the routing "
    "index. Pick a `skill_id` listed in the routing index in the "
    "system prompt; the response is INTERNAL guidance only -- it "
    "is wrapped in a `<skill_runbook>` envelope.  After loading, "
    "emit one tool call to satisfy the user's request immediately "
    "-- do not echo the runbook to the user and do not yield your "
    "turn back to the user before calling the tool."
)

_PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "skill_id": {
            "type": "string",
            "description": ("Path-style skill identifier exactly as listed in the routing index."),
        },
    },
    "required": ["skill_id"],
}

LoadSkillInvoke = Callable[[Any, str], Awaitable[str]]


def wrap_runbook(skill_id: str, name: str, body: str) -> str:
    """*body* inside the ``<skill_runbook>`` envelope the model is told to keep private."""
    return _OPENER_TEMPLATE.format(skill_id=skill_id, name=name) + body + _CLOSER + _POSTAMBLE


def load_skill_function_tool(on_invoke: LoadSkillInvoke) -> FunctionTool:
    """The ``_load_skill`` tool shape, around a source-specific *on_invoke*."""
    return FunctionTool(
        name=LOAD_SKILL_TOOL_NAME,
        description=_TOOL_DESCRIPTION,
        params_json_schema=_PARAMS_SCHEMA,
        on_invoke_tool=on_invoke,
        strict_json_schema=False,
    )


def load_skill_invoke(tools: Sequence[Any]) -> LoadSkillInvoke | None:
    """The ``on_invoke_tool`` of the ``_load_skill`` tool in *tools*, if any."""
    tool = next((tool for tool in tools if getattr(tool, "name", None) == LOAD_SKILL_TOOL_NAME), None)
    return tool.on_invoke_tool if tool is not None else None


def without_load_skill(tools: Sequence[Any]) -> list[Any]:
    return [tool for tool in tools if getattr(tool, "name", None) != LOAD_SKILL_TOOL_NAME]


def build_load_skill_tool(
    skill_index: dict[str, str],
    skills_root: Path,
) -> FunctionTool | None:
    """Construct the ``_load_skill`` FunctionTool from a preloaded index.

    Reads each ``<skills_root>/<rel_path>`` body once at factory time
    and stores it in a local dict closed over by the tool's invoke
    callback.  Skips (with a warning) any body file that is missing on
    disk so a partial deploy degrades gracefully rather than crashing
    the agent.

    Returns ``None`` when the resulting body dict is empty -- nothing
    useful to load means the tool would only ever return errors, so
    the runtime should not advertise it.
    """
    bodies, names = _preload_skill_bodies(skill_index, skills_root)
    if not bodies:
        return None

    valid_ids = sorted(bodies)

    async def _invoke(_ctx: Any, args_json: str) -> str:
        import mlflow

        args = json.loads(args_json) if args_json else {}
        skill_id = args.get("skill_id")
        if not isinstance(skill_id, str) or not skill_id:
            return "Error: 'skill_id' is required."

        body = bodies.get(skill_id)
        if body is None:
            return f"Error: skill_id {skill_id!r} not found. Valid skill_ids: {valid_ids!r}."

        with mlflow.start_span(
            name=LOAD_SKILL_TOOL_NAME,
            span_type="TOOL",
        ) as span:
            span.set_attribute(SKILL_LOADED_ATTRIBUTE, skill_id)
            span.set_inputs({"skill_id": skill_id})
            response = wrap_runbook(skill_id, names.get(skill_id, skill_id), body)
            span.set_outputs({"body_chars": len(body)})
            return response

    return load_skill_function_tool(_invoke)


def maybe_append_load_skill_tool(
    tools: list[FunctionTool],
    config: dict[str, Any],
    skills_root: Path,
) -> list[FunctionTool]:
    """Conditionally append the ``_load_skill`` tool to *tools*.

    No-op (returns *tools* unchanged) when:
    - the runtime is not in lazy mode (``SKILL_LOADING != "lazy"``), or
    - the skill index is missing / empty, or
    - every body file referenced by the index is missing on disk
      (``build_load_skill_tool`` returned ``None``).

    Returns a new list when something is appended; never mutates the
    input list, matching the immutable-list convention used by
    ``create_agent`` and ``build_all_tools``.
    """
    if config.get("SKILL_LOADING") != "lazy":
        return tools
    skill_index = config.get("SKILL_INDEX")
    if not skill_index:
        return tools
    load_skill = build_load_skill_tool(skill_index, skills_root)
    if load_skill is None:
        return tools
    return [*tools, load_skill]


def _preload_skill_bodies(
    skill_index: dict[str, str],
    skills_root: Path,
) -> tuple[dict[str, str], dict[str, str]]:
    """Read every body file referenced by the index into memory.

    Returns ``(bodies, names)`` -- two parallel dicts keyed on
    ``skill_id``: the body without frontmatter, and the skill's display
    name.  Missing or unparsable body files are skipped with a warning
    so partial deploys do not crash the agent at startup.
    """
    bodies: dict[str, str] = {}
    names: dict[str, str] = {}
    for skill_id, rel_path in skill_index.items():
        body_path = skills_root / rel_path
        if not body_path.exists():
            logger.warning(
                "[lazy_skills] skipping %r: body file missing at %s",
                skill_id,
                body_path,
            )
            continue
        text = body_path.read_text(encoding="utf-8")
        if body_path.name != SKILL_FILE:
            bodies[skill_id] = text
            names[skill_id] = _read_legacy_display_name(skill_id, body_path)
            continue
        try:
            document = parse_skill_md(text)
        except SkillDocumentError as exc:
            logger.warning("[lazy_skills] skipping %r: %s", skill_id, exc)
            continue
        bodies[skill_id] = document.body
        names[skill_id] = document.name
    return bodies, names


def _read_legacy_display_name(skill_id: str, body_path: Path) -> str:
    """Best-effort lookup of a legacy ``prompt.md``'s ``skill.yml#name`` for the preamble.

    Falls back to the trailing path component of ``skill_id`` when the
    sibling ``skill.yml`` is missing, malformed, or has no ``name``
    field.  Never raises -- a missing display name is purely cosmetic.
    """
    fallback = skill_id.rsplit("/", 1)[-1]
    skill_yml = body_path.parent / "skill.yml"
    if not skill_yml.exists():
        return fallback
    try:
        meta = yaml.safe_load(skill_yml.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return fallback
    if not isinstance(meta, dict):
        return fallback
    name = meta.get("name")
    return str(name) if isinstance(name, str) and name else fallback
