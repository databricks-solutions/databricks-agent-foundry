"""What the chat sidebar shows: the agent's resources and the user's catalog.

Two sections, built per request:

* ``agent`` -- what the agent always uses: gateway ``pinned`` entries and
  what the service principal's ``discovery`` finds, or, off the gateway,
  the ``server.yml`` MCP servers and Foundry skills.  Read-only in the UI.
* ``user`` -- what the signed-in user may add: their own listing of each
  ``user_catalog`` block's locations, filtered by its policy.  Listed with
  the user's token, so it only ever shows what the user can already see.

The user's saved picks are not part of the response: the UI merges them
in, and the agent re-checks every pick on every request anyway.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Literal

from databricks.sdk.errors.base import DatabricksError
from pydantic import BaseModel, ConfigDict

from payloads.agent.contracts import GatewayMcpRuntime, GatewaySkillsRuntime, Identity, UserSkills
from payloads.agent.runtime.gateway_mcp import client_for_identity, gateway_mcp_server
from payloads.agent.runtime.gateway_resolve import (
    Candidate,
    CatalogItem,
    PinnedSource,
    SelectedSource,
    SpDiscoverySource,
    get_mcp_service,
    get_skill,
    list_mcp_services,
    list_skills,
    policy_violation,
    resolve_within_limits,
    resource_kind,
)
from payloads.agent.runtime.gateway_selection import ResourceKind, SelectedResource, Selections
from payloads.agent.runtime.skill_authoring import AuthoringInfo, authoring_info

if TYPE_CHECKING:
    from databricks.sdk import WorkspaceClient

    from payloads.agent.runtime.gateway_resolve import GatewayRuntime

logger = logging.getLogger(__name__)

EntrySource = Literal["unity_gateway", "workspace", "foundry"]
EntryOrigin = Literal["pinned", "discovered", "configured", "user_catalog"]
UserReason = Literal["no_user_token", "listing_failed"]

_GETTERS: dict[ResourceKind, Callable[[WorkspaceClient, str], CatalogItem]] = {
    "mcp_service": get_mcp_service,
    "skill": get_skill,
}
_LISTERS: dict[ResourceKind, Callable[[WorkspaceClient, Sequence[str]], list[CatalogItem]]] = {
    "mcp_service": list_mcp_services,
    "skill": list_skills,
}


class ResourceEntry(BaseModel):
    """One MCP service or skill shown in the sidebar."""

    model_config = ConfigDict(frozen=True)

    kind: ResourceKind
    fqn: str
    display_name: str
    description: str = ""
    source: EntrySource
    identity: Identity
    origin: EntryOrigin
    # Why the details could not be read ("no_user_token", "unavailable").
    reason: str | None = None


class UserCatalogLimits(BaseModel):
    """How many picks the user may add; ``None`` when that kind cannot be picked."""

    max_servers: int | None = None
    max_tools: int | None = None
    max_skills: int | None = None


class ResourcesResponse(BaseModel):
    agent: list[ResourceEntry]
    user: list[ResourceEntry]
    limits: UserCatalogLimits
    user_reason: UserReason | None = None
    authoring: AuthoringInfo | None = None


class ToolInfo(BaseModel):
    name: str
    description: str = ""


class ToolsResponse(BaseModel):
    fqn: str
    tools: list[ToolInfo]


class ResourceNotAllowedError(LookupError):
    """The FQN is neither one of the agent's MCP services nor pickable by the user."""


class UserTokenRequiredError(PermissionError):
    """The MCP service runs as the user, and the request has no user token."""


def gateway_blocks(config: Mapping[str, Any]) -> list[GatewayRuntime]:
    blocks = (config.get("GATEWAY_MCP"), config.get("GATEWAY_SKILLS"))
    return [block for block in blocks if isinstance(block, GatewayMcpRuntime | GatewaySkillsRuntime)]


def _agent_candidates(gateway: GatewayRuntime, sp_ws: WorkspaceClient) -> tuple[Candidate, ...]:
    resolution = resolve_within_limits(
        gateway,
        pinned=PinnedSource(gateway).candidates(),
        discovered=SpDiscoverySource(gateway, sp_ws).candidates(),
    )
    return resolution.mounted


def _gateway_entry(
    kind: ResourceKind,
    candidate: Candidate,
    sp_ws: WorkspaceClient,
    user_ws: WorkspaceClient | None,
) -> ResourceEntry:
    entry = ResourceEntry(
        kind=kind,
        fqn=candidate.fqn,
        display_name=candidate.fqn.rsplit(".", 1)[-1],
        source="unity_gateway",
        identity=candidate.identity,
        origin="pinned" if candidate.origin == "pinned" else "discovered",
    )
    ws = client_for_identity(candidate.identity, sp_ws, user_ws)
    if ws is None:
        return entry.model_copy(update={"reason": "no_user_token"})
    try:
        item = _GETTERS[kind](ws, candidate.fqn)
    except DatabricksError as exc:
        logger.warning("[resources] cannot read %s %s: %s", kind, candidate.fqn, exc)
        return entry.model_copy(update={"reason": "unavailable"})
    return entry.model_copy(update={"display_name": item.display_name, "description": item.description})


def agent_entries(
    config: Mapping[str, Any],
    sp_ws: WorkspaceClient,
    user_ws: WorkspaceClient | None,
    workspace_mcp_names: Sequence[str] = (),
) -> list[ResourceEntry]:
    """The agent's own resources, in the order the agent uses them."""
    entries = [
        _gateway_entry(resource_kind(gateway), candidate, sp_ws, user_ws)
        for gateway in gateway_blocks(config)
        for candidate in _agent_candidates(gateway, sp_ws)
    ]
    entries.extend(
        ResourceEntry(
            kind="mcp_service", fqn=name, display_name=name, source="workspace", identity="obo", origin="configured"
        )
        for name in workspace_mcp_names
    )
    if config.get("REPOSITORY_SKILLS", True):
        entries.extend(
            ResourceEntry(
                kind="skill", fqn=name, display_name=name, source="foundry", identity="sp", origin="configured"
            )
            for name in config.get("SKILL_NAMES") or ()
        )
    return entries


def _user_catalog_entries(gateway: GatewayRuntime, user_ws: WorkspaceClient) -> list[ResourceEntry]:
    kind = resource_kind(gateway)
    spec = gateway.user_catalog
    return [
        ResourceEntry(
            kind=kind,
            fqn=item.fqn,
            display_name=item.display_name,
            description=item.description,
            source="unity_gateway",
            identity="obo",
            origin="user_catalog",
        )
        for item in _LISTERS[kind](user_ws, spec.locations)
        if policy_violation(item.fqn, spec) is None
    ]


def user_entries(
    config: Mapping[str, Any],
    user_ws: WorkspaceClient | None,
    agent_fqns: set[str],
) -> tuple[list[ResourceEntry], UserReason | None]:
    """What the user may pick, minus what the agent already uses."""
    blocks = [gateway for gateway in gateway_blocks(config) if gateway.user_catalog.enabled]
    if not blocks:
        return [], None
    if user_ws is None:
        return [], "no_user_token"
    entries: list[ResourceEntry] = []
    reason: UserReason | None = None
    for gateway in blocks:
        try:
            entries.extend(_user_catalog_entries(gateway, user_ws))
        except DatabricksError as exc:
            logger.warning("[resources] user catalog listing failed: %s", exc)
            reason = "listing_failed"
    return [entry for entry in entries if entry.fqn not in agent_fqns], reason


def user_catalog_limits(config: Mapping[str, Any]) -> UserCatalogLimits:
    limits = UserCatalogLimits()
    mcp = config.get("GATEWAY_MCP")
    if isinstance(mcp, GatewayMcpRuntime) and mcp.user_catalog.enabled:
        limits.max_servers = mcp.user_catalog.limits.max_servers
        limits.max_tools = mcp.user_catalog.limits.max_tools
    skills = config.get("GATEWAY_SKILLS")
    if isinstance(skills, GatewaySkillsRuntime) and skills.user_catalog.enabled:
        limits.max_skills = skills.user_catalog.limits.max_skills
    user_skills = config.get("USER_SKILLS")
    if isinstance(user_skills, UserSkills):
        limits.max_skills = user_skills.max_skills
    return limits


def build_resources(
    config: Mapping[str, Any],
    sp_ws: WorkspaceClient,
    user_ws: WorkspaceClient | None,
    workspace_mcp_names: Sequence[str] = (),
) -> ResourcesResponse:
    """Both sidebar sections for this user.  Blocking: run it off the event loop."""
    agent = agent_entries(config, sp_ws, user_ws, workspace_mcp_names)
    user, user_reason = user_entries(config, user_ws, {entry.fqn for entry in agent})
    return ResourcesResponse(
        agent=agent,
        user=user,
        limits=user_catalog_limits(config),
        user_reason=user_reason,
        authoring=authoring_info(config),
    )


def mcp_tools_candidate(config: Mapping[str, Any], fqn: str, sp_ws: WorkspaceClient) -> Candidate:
    """The candidate the agent would mount *fqn* as, for the agent or as a user pick.

    Raises :class:`ResourceNotAllowedError` for anything else, so the tools
    endpoint never connects to a service the agent itself would refuse.
    """
    gateway = config.get("GATEWAY_MCP")
    if not isinstance(gateway, GatewayMcpRuntime):
        raise ResourceNotAllowedError(fqn)
    for candidate in _agent_candidates(gateway, sp_ws):
        if candidate.fqn == fqn:
            return candidate
    if policy_violation(fqn, gateway.user_catalog) is not None:
        raise ResourceNotAllowedError(fqn)
    pick = Selections(resources=(SelectedResource(kind="mcp_service", fqn=fqn),))
    return SelectedSource(gateway, pick).candidates()[0]


async def list_mcp_tools(
    candidate: Candidate,
    sp_ws: WorkspaceClient,
    user_ws: WorkspaceClient | None,
    session_timeout_seconds: float,
) -> ToolsResponse:
    """Connect to one gateway MCP service as its identity and list its visible tools."""
    auth_ws = client_for_identity(candidate.identity, sp_ws, user_ws)
    if auth_ws is None:
        raise UserTokenRequiredError(candidate.fqn)
    server = gateway_mcp_server(candidate, sp_ws.config.host or "", auth_ws, session_timeout_seconds)
    async with server:
        tools = await server.list_tools()
    return ToolsResponse(
        fqn=candidate.fqn,
        tools=[ToolInfo(name=tool.name, description=tool.description or "") for tool in tools],
    )
