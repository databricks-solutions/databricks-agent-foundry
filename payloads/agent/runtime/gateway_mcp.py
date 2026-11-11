"""Unity Gateway MCP services (``spec.runtime.tools.mcp.gateway``).

Each resolved service (see :mod:`payloads.agent.runtime.gateway_resolve`)
is mounted at ``{host}/ai-gateway/mcp-services/{fqn}`` as its identity:

* ``sp`` always runs as the app's service principal, even when a user
  token is present.
* ``obo`` runs as the signed-in user.  On Databricks Apps, a request
  without a user token drops the service -- it never runs as the service
  principal.  Local development has no forwarded token and runs it as the
  developer's own CLI-profile client.

Tool-level rules need the tool lists, so :func:`enforce_tool_rules` runs
after connecting: a server whose tool name is already taken by a
workspace (``server.yml``) server or an earlier gateway server is dropped
whole, as is a discovered or selected server that would take its block
past ``limits.max_tools``.
"""

from __future__ import annotations

import fnmatch
import logging
import re
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

import mlflow
from databricks_mcp.oauth_provider import DatabricksOAuthClientProvider

from agent_foundry import env
from payloads.agent.contracts import SECURABLE_FQN_PATTERN
from payloads.agent.runtime.gateway_resolve import Candidate, Dropped, Origin, Resolution, config_hash, resolve_request
from payloads.agent.runtime.mcp_availability import McpBuild, UnavailableReason, UnavailableServer
from payloads.agent.runtime.mcp_tools_cache import SERVICE_PRINCIPAL_SCOPE
from payloads.agent.runtime.mcp_wrapper import FilteredMCPServerStreamableHttp

if TYPE_CHECKING:
    from agents import AgentBase, RunContextWrapper
    from databricks.sdk import WorkspaceClient
    from mcp.types import CallToolResult
    from mcp.types import Tool as MCPTool

    from agent_foundry.contracts.mcp import ToolAllowEntry
    from payloads.agent.contracts import GatewayMcpRuntime, Identity
    from payloads.agent.runtime.gateway_selection import Selections

logger = logging.getLogger(__name__)

_SECURABLE_FQN = re.compile(SECURABLE_FQN_PATTERN)

# FQNs already reported as dropped, so a missing token logs once per
# process instead of once per request.
_reported_drops: set[str] = set()

# Services someone chose -- in config or in the sidebar -- are reported as
# unavailable when dropped; discovered ones stay trace-only.
_REPORTED_ORIGINS: frozenset[Origin] = frozenset({"pinned", "selected"})


class GatewayMcpServer(FilteredMCPServerStreamableHttp):
    """A gateway MCP service, carrying the candidate it was resolved from.

    Tools matching the block's ``exclude_tools`` are hidden and refused,
    and the visible tools are sorted by name so the tool list -- part of
    the cached prompt prefix -- is stable.
    """

    def __init__(self, *args: Any, candidate: Candidate, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.candidate = candidate

    def _excluded(self, tool_name: str) -> bool:
        return any(fnmatch.fnmatchcase(tool_name, pattern) for pattern in self.candidate.exclude_tools)

    async def list_tools(
        self,
        run_context: RunContextWrapper[Any] | None = None,
        agent: AgentBase | None = None,
    ) -> list[MCPTool]:
        tools = await super().list_tools(run_context=run_context, agent=agent)
        return sorted((tool for tool in tools if not self._excluded(tool.name)), key=lambda tool: tool.name)

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None,
        meta: dict[str, Any] | None = None,
    ) -> CallToolResult:
        if self._excluded(tool_name):
            raise PermissionError(f"tool {tool_name!r} is excluded for {self.candidate.fqn}")
        return await super().call_tool(tool_name, arguments, meta)


def mcp_service_url(host: str, fqn: str) -> str:
    """Return the gateway URL for *fqn*, refusing anything but ``catalog.schema.name``."""
    if not _SECURABLE_FQN.fullmatch(fqn):
        raise ValueError(f"not a Unity Catalog MCP service name: {fqn!r}")
    return f"{host.rstrip('/')}/ai-gateway/mcp-services/{fqn}"


def client_for_identity(
    identity: Identity,
    sp_ws: WorkspaceClient,
    user_ws: WorkspaceClient | None,
) -> WorkspaceClient | None:
    """The client *identity* runs as, or ``None`` when it cannot run this request."""
    if identity == "sp":
        return sp_ws
    if user_ws is not None:
        return user_ws
    if env.on_databricks_apps():
        return None
    return sp_ws


def _allow_entries(candidate: Candidate) -> list[ToolAllowEntry] | None:
    """``tools.allow`` as wrapper entries; empty leaves the service in charge."""
    if not candidate.allow:
        return None
    from agent_foundry.contracts.mcp import ToolAllowEntry

    return [ToolAllowEntry(match=pattern) for pattern in candidate.allow]


def gateway_mcp_server(
    candidate: Candidate,
    host: str,
    auth_ws: WorkspaceClient,
    session_timeout_seconds: float,
    *,
    cache_scope: str = "",
) -> GatewayMcpServer:
    """An unconnected wrapper for *candidate*, authenticating as *auth_ws*.

    *cache_scope* keys its tool list in the process cache; empty leaves it uncached.
    """
    url = mcp_service_url(host, candidate.fqn)
    return GatewayMcpServer(
        params={"url": url, "auth": DatabricksOAuthClientProvider(auth_ws)},
        candidate=candidate,
        allow_entries=_allow_entries(candidate),
        server_url=url,
        cache_scope=cache_scope,
        cache_tools_list=True,
        name=f"gateway-{candidate.fqn}",
        client_session_timeout_seconds=session_timeout_seconds,
    )


def _cache_scope(identity: Identity, user_cache_scope: str) -> str:
    """The tool-cache scope of a service running as *identity*: the SP scope, or the caller's."""
    return SERVICE_PRINCIPAL_SCOPE if identity == "sp" else user_cache_scope


def _report_drop(fqn: str) -> None:
    if fqn in _reported_drops:
        return
    _reported_drops.add(fqn)
    logger.warning("[mcp] dropping gateway MCP service %s: identity 'obo' but the request has no user token", fqn)


def _labels(entries: Sequence[Candidate | Dropped]) -> list[str]:
    return [
        f"{e.fqn}|{e.identity}|{e.origin}" if isinstance(e, Candidate) else f"{e.fqn}|{e.origin}|{e.reason}"
        for e in entries
    ]


def resolution_attributes(
    gateway: GatewayMcpRuntime,
    resolution: Resolution,
    selections: Selections,
) -> dict[str, Any]:
    """Span attributes for one resolution: names, identities and reasons only."""
    attributes: dict[str, Any] = {
        "gateway.config_hash": config_hash(gateway),
        "gateway.mcp.mounted": _labels(resolution.mounted),
        "gateway.mcp.dropped": _labels(resolution.dropped),
    }
    if selections.rejected:
        attributes["gateway.selections.rejected"] = selections.rejected
    return attributes


def _without_token_drops(resolution: Resolution, mounted: Sequence[Candidate]) -> Resolution:
    kept = {candidate.fqn for candidate in mounted}
    no_token = tuple(Dropped(c.fqn, c.origin, "no_user_token") for c in resolution.mounted if c.fqn not in kept)
    return Resolution(mounted=tuple(mounted), dropped=(*resolution.dropped, *no_token))


def unavailable_service(candidate: Candidate, reason: UnavailableReason) -> UnavailableServer | None:
    """*candidate* as an unavailable server, or ``None`` when its origin is trace-only."""
    if candidate.origin not in _REPORTED_ORIGINS:
        return None
    return UnavailableServer(candidate.fqn, reason)


def build_gateway_mcp_servers(
    gateway: GatewayMcpRuntime,
    sp_ws: WorkspaceClient,
    user_ws: WorkspaceClient | None,
    *,
    selections: Selections,
    session_timeout_seconds: float,
    user_cache_scope: str = "",
) -> list[GatewayMcpServer]:
    """The servers of :func:`build_gateway_mcp_servers_with_status`."""
    return build_gateway_mcp_servers_with_status(
        gateway,
        sp_ws,
        user_ws,
        selections=selections,
        session_timeout_seconds=session_timeout_seconds,
        user_cache_scope=user_cache_scope,
    ).servers


def build_gateway_mcp_servers_with_status(
    gateway: GatewayMcpRuntime,
    sp_ws: WorkspaceClient,
    user_ws: WorkspaceClient | None,
    *,
    selections: Selections,
    session_timeout_seconds: float,
    user_cache_scope: str = "",
) -> McpBuild:
    """Unconnected wrappers for every service this request can run, in resolved order.

    ``unavailable`` lists the pinned and selected services dropped for a
    missing user token.  *user_cache_scope* keys the tool lists of services
    running as the user (see :func:`~payloads.agent.runtime.mcp_tools_cache.identity_scope`);
    empty leaves them uncached.
    """
    with mlflow.start_span(name="gateway.mcp_resolve", span_type="TASK") as span:
        resolution = resolve_request(gateway, sp_ws, selections)
        host = sp_ws.config.host or ""
        servers: list[GatewayMcpServer] = []
        unavailable: list[UnavailableServer] = []
        for candidate in resolution.mounted:
            auth_ws = client_for_identity(candidate.identity, sp_ws, user_ws)
            if auth_ws is None:
                _report_drop(candidate.fqn)
                if entry := unavailable_service(candidate, "no_user_token"):
                    unavailable.append(entry)
                continue
            server = gateway_mcp_server(
                candidate,
                host,
                auth_ws,
                session_timeout_seconds,
                cache_scope=_cache_scope(candidate.identity, user_cache_scope),
            )
            servers.append(server)
        final = _without_token_drops(resolution, [server.candidate for server in servers])
        span.set_attributes(resolution_attributes(gateway, final, selections))
    return McpBuild(servers=list(servers), unavailable=tuple(unavailable))


def _tool_limit(gateway: GatewayMcpRuntime, candidate: Candidate) -> int | None:
    if candidate.origin == "discovered":
        return gateway.discovery.limits.max_tools
    if candidate.origin == "selected":
        return gateway.user_catalog.limits.max_tools
    return None


async def enforce_tool_rules(gateway: GatewayMcpRuntime, servers: Sequence[Any]) -> list[Any]:
    """The servers kept by :func:`enforce_tool_rules_with_status`."""
    return (await enforce_tool_rules_with_status(gateway, servers)).servers


async def enforce_tool_rules_with_status(gateway: GatewayMcpRuntime, servers: Sequence[Any]) -> McpBuild:
    """Drop gateway servers with a colliding tool name or over their block's ``max_tools``.

    *servers* are connected and in resolved order, so pinned services
    claim their tool names first.  Non-gateway (workspace) servers are
    always kept and claim their tool names before any gateway server.
    ``unavailable`` lists the pinned and selected services dropped for a
    tool-name collision; limit drops are trace-only.
    """
    kept = [server for server in servers if not isinstance(server, GatewayMcpServer)]
    unavailable: list[UnavailableServer] = []
    taken = {tool.name for server in kept for tool in await server.list_tools()}
    tools_per_origin: dict[str, int] = {}
    with mlflow.start_span(name="gateway.mcp_tool_rules", span_type="TASK") as span:
        dropped: list[str] = []
        for server in servers:
            if not isinstance(server, GatewayMcpServer):
                continue
            candidate = server.candidate
            names = {tool.name for tool in await server.list_tools()}
            reason = _tool_rule_violation(gateway, candidate, names, taken, tools_per_origin)
            if reason is not None:
                logger.warning("[gateway] dropping MCP service %s: %s", candidate.fqn, reason)
                dropped.append(f"{candidate.fqn}|{candidate.origin}|{reason}")
                if reason == "tool_name_collision" and (entry := unavailable_service(candidate, "tool_name_collision")):
                    unavailable.append(entry)
                continue
            taken |= names
            tools_per_origin[candidate.origin] = tools_per_origin.get(candidate.origin, 0) + len(names)
            kept.append(server)
        span.set_attribute("gateway.mcp.tool_rule_drops", dropped)
    return McpBuild(servers=kept, unavailable=tuple(unavailable))


async def apply_gateway_tool_rules(config: Mapping[str, Any], servers: list[Any]) -> McpBuild:
    """:func:`enforce_tool_rules_with_status` with a gateway block; *servers* unchanged otherwise."""
    gateway = config.get("GATEWAY_MCP")
    if gateway is None:
        return McpBuild(servers=servers)
    return await enforce_tool_rules_with_status(gateway, servers)


def _tool_rule_violation(
    gateway: GatewayMcpRuntime,
    candidate: Candidate,
    names: set[str],
    taken: set[str],
    tools_per_origin: dict[str, int],
) -> str | None:
    if names & taken:
        return "tool_name_collision"
    limit = _tool_limit(gateway, candidate)
    if limit is not None and tools_per_origin.get(candidate.origin, 0) + len(names) > limit:
        return "over_tool_limit"
    return None
