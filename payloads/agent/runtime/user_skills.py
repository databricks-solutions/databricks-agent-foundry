"""Lakebase user skills (``spec.runtime.tools.skills.user_skills``).

The chat UI server reads the skills a user picked from Lakebase and sends
them, bodies included, as ``custom_inputs.user_skills``.  They are untrusted
input: shape, count and size are re-checked here against the deploy's
limits, and a malformed value is rejected whole.

They are listed by name and description after the identity sentinel,
outside the cached prefix, and ``_load_skill`` returns a body on demand.
``_load_skill`` also serves the agent's gateway skills and the repo's lazy
skills, by their ids.

``_save_skill`` lets the agent create or update one of the user's skills
(never delete).  It writes nothing: the chat UI server reads accepted calls
from the response stream and saves and picks them as the signed-in user.

Both tools are registered whenever user skills are on, even with nothing
picked, so the tool list -- part of the cached prefix -- is the same for
every user.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any

import mlflow
from agents import FunctionTool
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from agent_foundry.contracts.skills import MAX_SKILL_DESCRIPTION_CHARS, SKILL_SLUG
from payloads.agent.contracts import UserSkills
from payloads.agent.runtime.gateway_skills import GatewaySkills, parse_skill_id
from payloads.agent.runtime.lazy_skills import (
    LOAD_SKILL_TOOL_NAME,
    SKILL_LOADED_ATTRIBUTE,
    LoadSkillInvoke,
    load_skill_function_tool,
    wrap_runbook,
)

logger = logging.getLogger(__name__)

USER_SKILLS_INPUT = "user_skills"
USER_SKILL_PREFIX = "lakebase:"
MAX_USER_SKILL_INPUTS = 64
SAVE_SKILL_TOOL_NAME = "_save_skill"
# Mirrors the chat UI's server/src/lib/user-skills.ts; must stay at least as strict.
_MAX_SLUG_CHARS = 40
_FQN_PATTERN = rf"^lakebase:{SKILL_SLUG}$"

# Constant across users and deploys: tool definitions are part of the cached prefix.
_SAVE_SKILL_DESCRIPTION = (
    "Save one of the user's own skills: a reusable set of instructions they can pick "
    "in the Resources panel. A new slug creates the skill; an existing slug replaces "
    "its description and body. The skill is picked for the user and applies from "
    "their next message. The user's own skills appear in the skill index as "
    "`lakebase:<slug>`; pass that slug to update one. "
    "Call this only when the user asks to save or update a skill; "
    "skills cannot be deleted here."
)
_SAVE_SKILL_PARAMS: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "slug": {
            "type": "string",
            "description": "Lower-case letters and digits separated by single hyphens, at most 40 characters.",
        },
        "description": {
            "type": "string",
            "description": "When the agent should use the skill, at most 1024 characters.",
        },
        "body": {"type": "string", "description": "The skill's instructions, in markdown."},
    },
    "required": ["slug", "description", "body"],
}


class UserSkillInput(BaseModel):
    """One picked user skill as the chat UI server sends it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    fqn: str = Field(pattern=_FQN_PATTERN, max_length=len(USER_SKILL_PREFIX) + _MAX_SLUG_CHARS)
    display_name: str = Field(min_length=1, max_length=_MAX_SLUG_CHARS)
    description: str = Field(min_length=1, max_length=MAX_SKILL_DESCRIPTION_CHARS)
    body: str = Field(min_length=1)


class SkillDraftInput(BaseModel):
    """``_save_skill``'s arguments; the chat UI server re-checks them before saving."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    slug: str = Field(pattern=rf"^{SKILL_SLUG}$", max_length=_MAX_SLUG_CHARS)
    description: str = Field(min_length=1, max_length=MAX_SKILL_DESCRIPTION_CHARS)
    body: str = Field(min_length=1)


_INPUTS: TypeAdapter[list[UserSkillInput]] = TypeAdapter(
    Annotated[list[UserSkillInput], Field(max_length=MAX_USER_SKILL_INPUTS)]
)


@dataclass(frozen=True)
class ParsedUserSkills:
    """The skills within limits; ``dropped`` is ``fqn|reason``, ``rejected`` why the input was refused."""

    skills: tuple[UserSkillInput, ...] = ()
    dropped: tuple[str, ...] = ()
    rejected: str | None = None


def _drop_reason(skill: UserSkillInput, kept: Mapping[str, UserSkillInput], limits: UserSkills) -> str | None:
    if skill.fqn in kept:
        return "duplicate"
    if len(skill.body) > limits.max_skill_md_chars:
        return "over_char_limit"
    if len(kept) >= limits.max_skills:
        return "over_skill_limit"
    return None


def parse_user_skills(custom_inputs: Mapping[str, Any] | None, limits: UserSkills) -> ParsedUserSkills:
    """Read ``user_skills``; missing means none picked."""
    raw = (custom_inputs or {}).get(USER_SKILLS_INPUT)
    if raw is None:
        return ParsedUserSkills()
    try:
        inputs = _INPUTS.validate_python(raw)
    except ValidationError as exc:
        reason = f"invalid custom_inputs.{USER_SKILLS_INPUT} ({exc.error_count()} error(s))"
        logger.warning("[user-skills] ignoring input: %s", reason)
        return ParsedUserSkills(rejected=reason)
    kept: dict[str, UserSkillInput] = {}
    dropped: list[str] = []
    for skill in inputs:
        drop = _drop_reason(skill, kept, limits)
        if drop is None:
            kept[skill.fqn] = skill
        else:
            dropped.append(f"{skill.fqn}|{drop}")
    return ParsedUserSkills(skills=tuple(kept.values()), dropped=tuple(dropped))


class UserSkillLoader:
    """``_load_skill`` for one request: ``lakebase:`` ids from its skills, other ids from *fallback*."""

    def __init__(self, skills: Sequence[UserSkillInput], fallback: LoadSkillInvoke | None) -> None:
        self._skills = {skill.fqn: skill for skill in skills}
        self._fallback = fallback

    async def __call__(self, ctx: Any, args_json: str) -> str:
        skill_id = parse_skill_id(args_json) or ""
        if self._fallback is not None and not skill_id.startswith(USER_SKILL_PREFIX):
            return await self._fallback(ctx, args_json)
        skill = self._skills.get(skill_id)
        if skill is None:
            valid = sorted(self._skills)
            return f"Error: skill_id {skill_id!r} is not in the skill index. Valid skill_ids: {valid!r}."
        with mlflow.start_span(name=LOAD_SKILL_TOOL_NAME, span_type="TOOL") as span:
            span.set_attribute(SKILL_LOADED_ATTRIBUTE, skill.fqn)
            span.set_outputs({"body_chars": len(skill.body)})
            return wrap_runbook(skill.fqn, skill.display_name, skill.body)


def _body_hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]


def _draft_problem(draft: SkillDraftInput, limits: UserSkills) -> str | None:
    if not draft.description.strip():
        return "description must say when to use the skill"
    if not draft.body.strip():
        return "body must contain the instructions"
    if len(draft.body) > limits.max_skill_md_chars:
        return f"body is {len(draft.body)} characters; the limit is {limits.max_skill_md_chars}"
    return None


def _field_problem(error: Mapping[str, Any]) -> str:
    field = ".".join(str(part) for part in error["loc"]) or "input"
    return f"{field}: {error['msg']}"


def _validation_summary(exc: ValidationError) -> str:
    return "; ".join(_field_problem(error) for error in exc.errors())


class SkillSaver:
    """``_save_skill``: checks the draft and accepts it; the chat UI server saves it as the user."""

    def __init__(self, limits: UserSkills) -> None:
        self._limits = limits

    async def __call__(self, _ctx: Any, args_json: str) -> str:
        try:
            draft = SkillDraftInput.model_validate_json(args_json or "{}")
        except ValidationError as exc:
            return f"Error: the skill was not saved ({_validation_summary(exc)})."
        problem = _draft_problem(draft, self._limits)
        if problem is not None:
            return f"Error: the skill was not saved ({problem})."
        fqn = f"{USER_SKILL_PREFIX}{draft.slug}"
        with mlflow.start_span(name=SAVE_SKILL_TOOL_NAME, span_type="TOOL") as span:
            span.set_attribute("user_skills.saved", f"{fqn}|{_body_hash(draft.body)}")
        return (
            f"Saving skill `{fqn}` for the user. It is picked for them and applies from "
            "their next message; the chat tells them whether it was saved."
        )


def save_skill_function_tool(limits: UserSkills) -> FunctionTool:
    return FunctionTool(
        name=SAVE_SKILL_TOOL_NAME,
        description=_SAVE_SKILL_DESCRIPTION,
        params_json_schema=_SAVE_SKILL_PARAMS,
        on_invoke_tool=SkillSaver(limits),
        strict_json_schema=False,
    )


def _span_attributes(parsed: ParsedUserSkills) -> dict[str, Any]:
    """Names, body hashes and reasons only -- never skill text."""
    attributes: dict[str, Any] = {
        "user_skills.indexed": [f"{skill.fqn}|{_body_hash(skill.body)}" for skill in parsed.skills],
        "user_skills.dropped": list(parsed.dropped),
    }
    if parsed.rejected:
        attributes["user_skills.rejected"] = parsed.rejected
    return attributes


def prepare_user_skills(
    config: Mapping[str, Any],
    custom_inputs: Mapping[str, Any] | None,
    agent_skills: GatewaySkills,
    fallback: LoadSkillInvoke | None,
) -> GatewaySkills | None:
    """*agent_skills* plus the user's index rows and ``_load_skill`` / ``_save_skill``; ``None`` when off.

    The ``_load_skill`` replaces any in *agent_skills*; ids that are not
    ``lakebase:`` go to *fallback*.
    """
    limits = config.get("USER_SKILLS")
    if not isinstance(limits, UserSkills):
        return None
    with mlflow.start_span(name="user_skills.prepare", span_type="TASK") as span:
        parsed = parse_user_skills(custom_inputs, limits)
        span.set_attributes(_span_attributes(parsed))
    rows = tuple((skill.fqn, skill.display_name, skill.description) for skill in parsed.skills)
    return GatewaySkills(
        block=agent_skills.block,
        rows=(*agent_skills.rows, *rows),
        tools=(load_skill_function_tool(UserSkillLoader(parsed.skills, fallback)), save_skill_function_tool(limits)),
    )
