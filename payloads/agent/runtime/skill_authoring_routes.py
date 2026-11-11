"""HTTP routes for users authoring skills (``skills.gateway.authoring``).

* ``POST /api/agent/skills`` -- create ``u-<handle>-<slug>`` in the authoring schema.
* ``PUT /api/agent/skills/{fqn}`` -- replace its description and body.
* ``DELETE /api/agent/skills/{fqn}`` -- delete it.

Mounted with the resources sidebar.  Every call needs the user's token and
runs as the user; Unity Catalog's answer (403, 404, 409, ...) is passed
through with its status and message.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, TypeVar

from databricks.sdk.errors import (
    BadRequest,
    NotFound,
    PermissionDenied,
    ResourceConflict,
    Unauthenticated,
)
from databricks.sdk.errors.base import DatabricksError
from fastapi import HTTPException, Request, Response

from payloads.agent.runtime.request_context import get_workspace_client
from payloads.agent.runtime.resources_routes import user_ws_from_request
from payloads.agent.runtime.runtime_config import load_agent_config_and_vars
from payloads.agent.runtime.skill_authoring import (
    AuthoredSkill,
    AuthoringDisabledError,
    AuthoringError,
    AuthoringUnavailableError,
    SkillDraft,
    SkillEdit,
    authoring_settings,
    authoring_unavailable_reason,
    create_user_skill,
    delete_user_skill,
    update_user_skill,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from databricks.sdk import WorkspaceClient
    from fastapi import FastAPI

    from payloads.agent.contracts import SkillAuthoring

SKILLS_PATH = "/api/agent/skills"
# The SDK appends its client config (host, account, client id, ...) to some
# error messages; that stays in the server log.
_SDK_DEBUG_SUFFIX = " Config: "
logger = logging.getLogger(__name__)
_T = TypeVar("_T")
_UC_ERROR_STATUS: tuple[tuple[type[DatabricksError], int], ...] = (
    (BadRequest, 400),
    (Unauthenticated, 401),
    (PermissionDenied, 403),
    (NotFound, 404),
    (ResourceConflict, 409),
)


def _uc_message(exc: DatabricksError) -> str:
    return str(exc).split(_SDK_DEBUG_SUFFIX, 1)[0]


def _uc_status(exc: DatabricksError) -> int:
    return next((status for cls, status in _UC_ERROR_STATUS if isinstance(exc, cls)), 502)


def _require_user_ws(request: Request) -> WorkspaceClient:
    user_ws = user_ws_from_request(request)
    if user_ws is None:
        raise HTTPException(status_code=401, detail="Sign in to manage your skills")
    return user_ws


def _authoring() -> SkillAuthoring:
    config, _ = load_agent_config_and_vars(get_workspace_client())
    authoring = authoring_settings(config)
    reason = authoring_unavailable_reason()
    if reason is not None:
        raise AuthoringUnavailableError(reason)
    return authoring


def _create(user_ws: WorkspaceClient, draft: SkillDraft) -> AuthoredSkill:
    return create_user_skill(user_ws, _authoring(), draft)


def _update(user_ws: WorkspaceClient, fqn: str, edit: SkillEdit) -> AuthoredSkill:
    return update_user_skill(user_ws, _authoring(), fqn, edit)


def _delete(user_ws: WorkspaceClient, fqn: str) -> None:
    delete_user_skill(user_ws, _authoring(), fqn)


async def _call(operation: Callable[..., _T], *args: object) -> _T:
    try:
        return await asyncio.to_thread(operation, *args)
    except AuthoringDisabledError:
        raise HTTPException(status_code=404, detail="Skill authoring is not enabled") from None
    except AuthoringUnavailableError as exc:
        raise HTTPException(status_code=501, detail=exc.reason) from None
    except AuthoringError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except DatabricksError as exc:
        logger.warning("Skill authoring call failed: %s", exc)
        raise HTTPException(status_code=_uc_status(exc), detail=_uc_message(exc)) from None


async def create_skill(request: Request, draft: SkillDraft) -> AuthoredSkill:
    return await _call(_create, _require_user_ws(request), draft)


async def update_skill(request: Request, fqn: str, edit: SkillEdit) -> AuthoredSkill:
    return await _call(_update, _require_user_ws(request), fqn, edit)


async def delete_skill(request: Request, fqn: str) -> Response:
    await _call(_delete, _require_user_ws(request), fqn)
    return Response(status_code=204)


def register_skill_authoring_routes(app: FastAPI) -> None:
    app.add_api_route(SKILLS_PATH, create_skill, methods=["POST"], response_model=AuthoredSkill, status_code=201)
    app.add_api_route(f"{SKILLS_PATH}/{{fqn}}", update_skill, methods=["PUT"], response_model=AuthoredSkill)
    app.add_api_route(f"{SKILLS_PATH}/{{fqn}}", delete_skill, methods=["DELETE"], status_code=204)
