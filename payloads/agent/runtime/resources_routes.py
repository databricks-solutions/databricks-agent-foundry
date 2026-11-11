"""HTTP routes for the chat sidebar (opt-in via ``ENABLE_RESOURCES_SIDEBAR``).

* ``GET /api/agent/resources`` -- the agent's resources, the user's
  catalog and the pick limits (see :mod:`gateway_resources`).
* ``GET /api/agent/resources/tools?fqn=`` -- the tools of one gateway MCP
  service, listed as the identity the agent would use for it.

Both act as the signed-in user via the ``x-forwarded-access-token`` header
Databricks Apps injects; without it the user section stays empty.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from fastapi import HTTPException, Query, Request

from payloads.agent.runtime.brick_mcp_servers import mcp_client_session_timeout_seconds, workspace_mcp_server_names
from payloads.agent.runtime.gateway_resources import (
    ResourceNotAllowedError,
    ResourcesResponse,
    ToolsResponse,
    UserTokenRequiredError,
    build_resources,
    list_mcp_tools,
    mcp_tools_candidate,
)
from payloads.agent.runtime.request_context import get_workspace_client, user_workspace_client
from payloads.agent.runtime.runtime_config import load_agent_config_and_vars

if TYPE_CHECKING:
    from databricks.sdk import WorkspaceClient
    from fastapi import FastAPI

    from payloads.agent.runtime.gateway_resolve import Candidate

logger = logging.getLogger(__name__)

RESOURCES_PATH = "/api/agent/resources"
TOOLS_PATH = "/api/agent/resources/tools"
_OBO_HEADER = "x-forwarded-access-token"
_MAX_FQN_CHARS = 512


def user_ws_from_request(request: Request) -> WorkspaceClient | None:
    token = request.headers.get(_OBO_HEADER)
    return user_workspace_client(token) if token else None


def _resources(user_ws: WorkspaceClient | None) -> ResourcesResponse:
    sp_ws = get_workspace_client()
    config, _ = load_agent_config_and_vars(sp_ws)
    return build_resources(config, sp_ws, user_ws, workspace_mcp_server_names(sp_ws))


def _tools_candidate(fqn: str) -> tuple[Candidate, WorkspaceClient]:
    sp_ws = get_workspace_client()
    config, _ = load_agent_config_and_vars(sp_ws)
    return mcp_tools_candidate(config, fqn, sp_ws), sp_ws


async def get_resources(request: Request) -> ResourcesResponse:
    return await asyncio.to_thread(_resources, user_ws_from_request(request))


async def get_resource_tools(
    request: Request,
    fqn: str = Query(min_length=1, max_length=_MAX_FQN_CHARS),
) -> ToolsResponse:
    try:
        candidate, sp_ws = await asyncio.to_thread(_tools_candidate, fqn)
        return await list_mcp_tools(
            candidate, sp_ws, user_ws_from_request(request), mcp_client_session_timeout_seconds()
        )
    except ResourceNotAllowedError:
        raise HTTPException(status_code=404, detail="Unknown MCP service") from None
    except UserTokenRequiredError:
        raise HTTPException(status_code=401, detail="This MCP service needs a signed-in user") from None
    except Exception:
        # Connecting to an MCP service fails in many transport-specific ways.
        logger.exception("[resources] listing tools failed for %s", fqn)
        raise HTTPException(status_code=502, detail="Cannot list tools for this MCP service") from None


def register_resources_routes(app: FastAPI) -> None:
    app.add_api_route(RESOURCES_PATH, get_resources, methods=["GET"], response_model=ResourcesResponse)
    app.add_api_route(TOOLS_PATH, get_resource_tools, methods=["GET"], response_model=ToolsResponse)
