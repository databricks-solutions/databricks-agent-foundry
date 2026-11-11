"""Users creating, updating and deleting their own Unity Gateway skills.

Enabled by ``skills.gateway.authoring``.  Every call runs as the signed-in
user, never as the app's service principal: the user owns the skills they
create, and Unity Catalog decides who may update or delete one (``MANAGE``).

New skills are named ``u-<handle>-<slug>`` in the authoring schema, where
*handle* comes from the user's name.  ``SKILL.md`` is generated here: the
frontmatter from the validated fields, the body as the user wrote it.
"""

from __future__ import annotations

import io
import logging
import re
import unicodedata
from typing import TYPE_CHECKING, Literal

from databricks.sdk.errors.base import DatabricksError
from databricks.sdk.service.catalog import Skill
from pydantic import BaseModel, ConfigDict, Field

from agent_foundry import env
from agent_foundry.contracts.skills import MAX_SKILL_DESCRIPTION_CHARS, SKILL_SLUG
from agent_foundry.contracts.skills import render_skill_md as render_document
from payloads.agent.contracts import SECURABLE_FQN_PATTERN, GatewaySkillsRuntime, UserSkills
from payloads.agent.runtime.gateway_resolve import skill_item, skill_resource_name
from payloads.agent.runtime.gateway_skills import skill_md_path

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import Any

    from databricks.sdk import WorkspaceClient

    from payloads.agent.contracts import SkillAuthoring

logger = logging.getLogger(__name__)

SLUG_PATTERN = rf"^{SKILL_SLUG}$"
MAX_SLUG_CHARS = 40
MAX_HANDLE_CHARS = 20
MAX_DESCRIPTION_CHARS = MAX_SKILL_DESCRIPTION_CHARS
_SKILL_ID_PREFIX = "u"
_NON_ID_CHARS = re.compile(r"[^a-z0-9]+")
_SECURABLE_FQN = re.compile(SECURABLE_FQN_PATTERN)


class SkillDraft(BaseModel):
    """What a user submits for a new skill."""

    model_config = ConfigDict(extra="forbid")

    slug: str = Field(pattern=SLUG_PATTERN, max_length=MAX_SLUG_CHARS)
    description: str = Field(min_length=1, max_length=MAX_DESCRIPTION_CHARS)
    body: str = Field(min_length=1)


class SkillEdit(BaseModel):
    """What a user submits to replace a skill's description and body."""

    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=1, max_length=MAX_DESCRIPTION_CHARS)
    body: str = Field(min_length=1)


class AuthoredSkill(BaseModel):
    fqn: str
    display_name: str
    description: str


# Databricks Apps cannot grant the ``unity-catalog`` OAuth scope the skills
# API requires, so OBO authoring fails there (a read-only variant is under
# security review).
AuthoringUnavailableReason = Literal["apps_unity_catalog_scope"]


class AuthoringInfo(BaseModel):
    """What the UI needs to offer (or grey out) the create form."""

    schema_fqn: str
    max_skill_md_chars: int
    unavailable_reason: AuthoringUnavailableReason | None = None
    # Where new skills go: a Unity Catalog schema (``skills.gateway.authoring``)
    # or the user's Lakebase skills (``skills.user_skills``, served by the chat UI).
    store: Literal["unity_catalog", "lakebase"] = "unity_catalog"


class AuthoringError(ValueError):
    """The request breaks an authoring rule; safe to show to the user."""


class AuthoringDisabledError(LookupError):
    """``skills.gateway.authoring`` is off."""


class AuthoringUnavailableError(RuntimeError):
    """Authoring is configured but this host cannot run it as the user."""

    def __init__(self, reason: AuthoringUnavailableReason) -> None:
        super().__init__(reason)
        self.reason = reason


def authoring_unavailable_reason() -> AuthoringUnavailableReason | None:
    return "apps_unity_catalog_scope" if env.read_optional(env.DATABRICKS_APP_NAME) else None


def authoring_settings(config: Mapping[str, Any]) -> SkillAuthoring:
    """The enabled authoring block; raises :class:`AuthoringDisabledError` otherwise."""
    gateway = config.get("GATEWAY_SKILLS")
    if not isinstance(gateway, GatewaySkillsRuntime) or not gateway.authoring.enabled:
        raise AuthoringDisabledError("skill authoring is not enabled")
    return gateway.authoring


def authoring_info(config: Mapping[str, Any]) -> AuthoringInfo | None:
    user_skills = config.get("USER_SKILLS")
    if isinstance(user_skills, UserSkills):
        return AuthoringInfo(schema_fqn="", max_skill_md_chars=user_skills.max_skill_md_chars, store="lakebase")
    try:
        authoring = authoring_settings(config)
    except AuthoringDisabledError:
        return None
    return AuthoringInfo(
        schema_fqn=authoring.schema_name or "",
        max_skill_md_chars=authoring.max_skill_md_chars,
        unavailable_reason=authoring_unavailable_reason(),
    )


def user_handle(user_name: str) -> str:
    """A skill-ID-safe handle from a user name such as ``first.last+x@corp.com``."""
    local = user_name.split("@", 1)[0]
    ascii_only = unicodedata.normalize("NFKD", local).encode("ascii", "ignore").decode("ascii")
    handle = _NON_ID_CHARS.sub("-", ascii_only.lower()).strip("-")
    handle = handle[:MAX_HANDLE_CHARS].rstrip("-")
    if not handle:
        raise AuthoringError(f"cannot derive a skill name prefix from user {user_name!r}")
    return handle


def skill_id_for(handle: str, slug: str) -> str:
    return f"{_SKILL_ID_PREFIX}-{handle}-{slug}"


def render_skill_md(skill_id: str, description: str, body: str) -> str:
    """``SKILL.md`` for a user's skill, its description on one line."""
    return render_document(skill_id, " ".join(description.split()), body)


def _check_size(content: str, authoring: SkillAuthoring) -> None:
    if len(content) > authoring.max_skill_md_chars:
        raise AuthoringError(f"SKILL.md would have {len(content)} chars, over the {authoring.max_skill_md_chars} limit")


def authored_fqn(authoring: SkillAuthoring, fqn: str) -> str:
    """*fqn*, provided it names a skill in the authoring schema."""
    if not _SECURABLE_FQN.fullmatch(fqn) or fqn.rsplit(".", 1)[0] != authoring.schema_name:
        raise AuthoringError("only skills in the authoring schema can be changed here")
    return fqn


def _publish(user_ws: WorkspaceClient, fqn: str, content: str) -> AuthoredSkill:
    user_ws.files.upload(skill_md_path(fqn), io.BytesIO(content.encode("utf-8")), overwrite=True)
    item = skill_item(user_ws.ai_gateway.finalize_skill(name=skill_resource_name(fqn)))
    return AuthoredSkill(fqn=fqn, display_name=item.display_name, description=item.description)


def _delete_quietly(user_ws: WorkspaceClient, fqn: str) -> None:
    try:
        user_ws.ai_gateway.delete_skill(name=skill_resource_name(fqn))
    except DatabricksError as exc:
        logger.warning("[authoring] could not remove half-created skill %s: %s", fqn, exc)


def create_user_skill(user_ws: WorkspaceClient, authoring: SkillAuthoring, draft: SkillDraft) -> AuthoredSkill:
    """Create, upload and finalize a new skill as the user; a later failure deletes it."""
    me = user_ws.current_user.me()
    skill_id = skill_id_for(user_handle(me.user_name or ""), draft.slug)
    schema_fqn = authoring.schema_name or ""
    fqn = f"{schema_fqn}.{skill_id}"
    content = render_skill_md(skill_id, draft.description, draft.body)
    _check_size(content, authoring)

    user_ws.ai_gateway.create_skill(
        Skill(comment=" ".join(draft.description.split())),
        parent=f"schemas/{schema_fqn}",
        skill_id=skill_id,
    )
    try:
        return _publish(user_ws, fqn, content)
    except DatabricksError:
        _delete_quietly(user_ws, fqn)
        raise


def update_user_skill(
    user_ws: WorkspaceClient,
    authoring: SkillAuthoring,
    fqn: str,
    edit: SkillEdit,
) -> AuthoredSkill:
    """Replace a skill's ``SKILL.md`` as the user; Unity Catalog checks the right to."""
    authored_fqn(authoring, fqn)
    content = render_skill_md(fqn.rsplit(".", 1)[-1], edit.description, edit.body)
    _check_size(content, authoring)
    return _publish(user_ws, fqn, content)


def delete_user_skill(user_ws: WorkspaceClient, authoring: SkillAuthoring, fqn: str) -> None:
    """Delete a skill as the user; Unity Catalog checks the right to."""
    authored_fqn(authoring, fqn)
    user_ws.ai_gateway.delete_skill(name=skill_resource_name(fqn))
