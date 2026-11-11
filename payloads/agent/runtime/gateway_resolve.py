"""Resolve which Unity Gateway MCP services or skills a request uses.

The gateway block's type decides the resource kind: a
``GatewayMcpRuntime`` resolves MCP services, a ``GatewaySkillsRuntime``
resolves skills.  Three sources feed one :func:`resolve` step:

* :class:`PinnedSource` -- ``gateway.pinned``, always used.
* :class:`SpDiscoverySource` -- ``gateway.discovery``, listed as the app's
  service principal and cached for :data:`LISTING_TTL_SECONDS`.
* :class:`SelectedSource` -- the user's sidebar picks.  Nothing is listed:
  each pick is checked statically against ``gateway.user_catalog`` and then
  used as the user, so the user's own grants are the final check.

:func:`resolve` applies the policy, drops duplicates (pinned wins),
enforces the count limits and fixes the order: pinned, then discovered,
then selected, each sorted by FQN.  Tool-level checks (name collisions,
``max_tools``) need the tool lists and run after connecting.
"""

from __future__ import annotations

import fnmatch
import hashlib
import logging
import re
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

from databricks.sdk.errors.base import DatabricksError

from payloads.agent.contracts import (
    SECURABLE_FQN_PATTERN,
    GatewayMcpRuntime,
    GatewaySkillsRuntime,
    McpDiscoverySpec,
    PinnedMcp,
    SkillDiscoverySpec,
)

if TYPE_CHECKING:
    from databricks.sdk import WorkspaceClient
    from databricks.sdk.service.catalog import McpService, Skill

    from payloads.agent.contracts import DiscoverySpec, Identity
    from payloads.agent.runtime.gateway_selection import ResourceKind, Selections

logger = logging.getLogger(__name__)

LISTING_TTL_SECONDS = 300.0
_MCP_SERVICE_NAME_PREFIX = "mcp-services/"
_SKILL_NAME_PREFIX = "skills/"
_SECURABLE_FQN = re.compile(SECURABLE_FQN_PATTERN)

Origin = Literal["pinned", "discovered", "selected"]
GatewayRuntime = GatewayMcpRuntime | GatewaySkillsRuntime


@dataclass(frozen=True)
class Candidate:
    """A gateway MCP service or skill a source offers."""

    fqn: str
    identity: Identity
    origin: Origin
    allow: tuple[str, ...] = ()
    exclude_tools: tuple[str, ...] = ()


@dataclass(frozen=True)
class Dropped:
    """A candidate left out, and why."""

    fqn: str
    origin: Origin
    reason: str


@dataclass(frozen=True)
class Resolution:
    mounted: tuple[Candidate, ...]
    dropped: tuple[Dropped, ...]


class GatewayLimitError(ValueError):
    """The service principal's discovery found more resources than its limit allows."""


class ResourceSource(Protocol):
    def candidates(self) -> list[Candidate]: ...


def resource_kind(gateway: GatewayRuntime) -> ResourceKind:
    return "mcp_service" if isinstance(gateway, GatewayMcpRuntime) else "skill"


def _exclude_tools(spec: DiscoverySpec) -> tuple[str, ...]:
    return tuple(spec.exclude_tools) if isinstance(spec, McpDiscoverySpec) else ()


def _count_limit(spec: DiscoverySpec) -> tuple[str, int]:
    """The ``limits`` field capping how many resources *spec* admits, and its value."""
    if isinstance(spec, McpDiscoverySpec):
        return "max_servers", spec.limits.max_servers
    if isinstance(spec, SkillDiscoverySpec):
        return "max_skills", spec.limits.max_skills
    raise TypeError(f"unsupported discovery block: {type(spec).__name__}")


class PinnedSource:
    def __init__(self, gateway: GatewayRuntime) -> None:
        self._gateway = gateway

    def candidates(self) -> list[Candidate]:
        return [
            Candidate(p.fqn, p.identity, "pinned", allow=tuple(p.tools.allow) if isinstance(p, PinnedMcp) else ())
            for p in self._gateway.pinned
        ]


# (kind, principal, discovery config hash) -> (expires_at, FQNs).  FQNs
# only: never tokens or connected wrappers.
_LISTING_CACHE: dict[tuple[str, str, str], tuple[float, tuple[str, ...]]] = {}


def clear_listing_cache() -> None:
    _LISTING_CACHE.clear()


@dataclass(frozen=True)
class CatalogItem:
    """A gateway MCP service or skill, with what a user is shown for it."""

    fqn: str
    display_name: str
    description: str


def mcp_service_item(service: McpService) -> CatalogItem:
    fqn = (service.name or "").removeprefix(_MCP_SERVICE_NAME_PREFIX)
    return CatalogItem(fqn, fqn.rsplit(".", 1)[-1], service.comment or "")


def skill_item(skill: Skill) -> CatalogItem:
    fqn = (skill.name or "").removeprefix(_SKILL_NAME_PREFIX)
    return CatalogItem(fqn, skill.bundle_name or fqn.rsplit(".", 1)[-1], skill.description or skill.comment or "")


def get_mcp_service(ws: WorkspaceClient, fqn: str) -> CatalogItem:
    return mcp_service_item(ws.ai_gateway.get_mcp_service(name=f"{_MCP_SERVICE_NAME_PREFIX}{fqn}"))


def skill_resource_name(fqn: str) -> str:
    """The ``name`` the skills API takes: ``skills/<catalog>.<schema>.<skill_id>``."""
    return f"{_SKILL_NAME_PREFIX}{fqn}"


def get_skill(ws: WorkspaceClient, fqn: str) -> CatalogItem:
    return skill_item(ws.ai_gateway.get_skill(name=skill_resource_name(fqn)))


def _unique_sorted(items: Iterable[CatalogItem]) -> list[CatalogItem]:
    return sorted({item.fqn: item for item in items}.values(), key=lambda item: item.fqn)


def list_mcp_services(ws: WorkspaceClient, locations: Iterable[str]) -> list[CatalogItem]:
    """The MCP services in *locations* visible to *ws*, sorted by FQN."""
    return _unique_sorted(
        mcp_service_item(service)
        for location in locations
        for service in ws.ai_gateway.list_mcp_services(parent=f"schemas/{location}")
        if service.name
    )


def list_skills(ws: WorkspaceClient, locations: Iterable[str]) -> list[CatalogItem]:
    """The skills in *locations* visible to *ws*, sorted by FQN."""
    return _unique_sorted(
        skill_item(skill)
        for location in locations
        for skill in ws.ai_gateway.list_skills(parent=f"schemas/{location}")
        if skill.name
    )


def list_mcp_service_fqns(ws: WorkspaceClient, locations: Iterable[str]) -> list[str]:
    """Sorted FQNs of the MCP services in *locations* visible to *ws*."""
    return [item.fqn for item in list_mcp_services(ws, locations)]


def list_skill_fqns(ws: WorkspaceClient, locations: Iterable[str]) -> list[str]:
    """Sorted FQNs of the skills in *locations* visible to *ws*."""
    return [item.fqn for item in list_skills(ws, locations)]


_LISTERS: dict[ResourceKind, Callable[[WorkspaceClient, Iterable[str]], list[str]]] = {
    "mcp_service": list_mcp_service_fqns,
    "skill": list_skill_fqns,
}


def config_hash(spec: DiscoverySpec | GatewayRuntime) -> str:
    """Short, stable hash of a gateway block for cache keys and traces."""
    return hashlib.sha256(spec.model_dump_json().encode("utf-8")).hexdigest()[:16]


class SpDiscoverySource:
    """Resources the app's service principal finds in ``discovery.locations``."""

    def __init__(
        self,
        gateway: GatewayRuntime,
        sp_ws: WorkspaceClient,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._kind = resource_kind(gateway)
        self._spec: DiscoverySpec = gateway.discovery
        self._sp_ws = sp_ws
        self._clock = clock

    def candidates(self) -> list[Candidate]:
        """:meth:`fetch`, with a listing failure logged and treated as nothing found."""
        try:
            return self.fetch()
        except DatabricksError as exc:
            logger.warning("[gateway] %s discovery failed; using pinned entries only: %s", self._kind, exc)
            return []

    def fetch(self) -> list[Candidate]:
        """The discovered candidates; raises ``DatabricksError`` when listing fails."""
        if not self._spec.enabled:
            return []
        exclude_tools = _exclude_tools(self._spec)
        return [Candidate(fqn, "sp", "discovered", exclude_tools=exclude_tools) for fqn in self._cached_fqns()]

    def _cached_fqns(self) -> tuple[str, ...]:
        key = (self._kind, self._sp_ws.config.client_id or "sp", config_hash(self._spec))
        now = self._clock()
        hit = _LISTING_CACHE.get(key)
        if hit is not None and hit[0] > now:
            return hit[1]
        fqns = tuple(_LISTERS[self._kind](self._sp_ws, self._spec.locations))
        _LISTING_CACHE[key] = (now + LISTING_TTL_SECONDS, fqns)
        return fqns


class SelectedSource:
    """The user's picks of the gateway block's kind, as OBO candidates.  Lists nothing."""

    def __init__(self, gateway: GatewayRuntime, selections: Selections) -> None:
        self._kind = resource_kind(gateway)
        self._exclude_tools = _exclude_tools(gateway.user_catalog)
        self._selections = selections

    def candidates(self) -> list[Candidate]:
        return [
            Candidate(pick.fqn, "obo", "selected", exclude_tools=self._exclude_tools)
            for pick in self._selections.of_kind(self._kind)
        ]


def _matches_any(fqn: str, patterns: Sequence[str]) -> bool:
    return any(fqn == pattern or fnmatch.fnmatchcase(fqn, pattern) for pattern in patterns)


def policy_violation(fqn: str, spec: DiscoverySpec) -> str | None:
    """Why *fqn* may not be used under *spec*, or ``None`` when it may."""
    if not _SECURABLE_FQN.fullmatch(fqn):
        return "invalid_fqn"
    if not spec.enabled:
        return "disabled"
    if fqn.rsplit(".", 1)[0] not in spec.locations:
        return "outside_locations"
    if spec.include and not _matches_any(fqn, spec.include):
        return "not_included"
    if _matches_any(fqn, spec.exclude):
        return "excluded"
    return None


def _admit(
    candidates: Iterable[Candidate],
    spec: DiscoverySpec,
    seen: set[str],
    dropped: list[Dropped],
) -> list[Candidate]:
    admitted: list[Candidate] = []
    for candidate in sorted(candidates, key=lambda c: c.fqn):
        if candidate.fqn in seen:
            continue
        reason = policy_violation(candidate.fqn, spec)
        if reason is not None:
            dropped.append(Dropped(candidate.fqn, candidate.origin, reason))
            continue
        seen.add(candidate.fqn)
        admitted.append(candidate)
    return admitted


def resolve(
    gateway: GatewayRuntime,
    *,
    pinned: Iterable[Candidate],
    discovered: Iterable[Candidate] = (),
    selected: Iterable[Candidate] = (),
) -> Resolution:
    """Policy, de-duplication, limits and ordering for one request.

    Raises :class:`GatewayLimitError` when discovery exceeds its count
    limit (``max_servers`` / ``max_skills``) -- a deployer
    misconfiguration.  Selections over the ``user_catalog`` limit are user
    input, so they are all dropped (reason ``over_limit``) instead.
    """
    dropped: list[Dropped] = []
    mounted = sorted(pinned, key=lambda c: c.fqn)
    seen = {candidate.fqn for candidate in mounted}

    sp_found = _admit(discovered, gateway.discovery, seen, dropped)
    field, limit = _count_limit(gateway.discovery)
    if len(sp_found) > limit:
        raise GatewayLimitError(
            f"{resource_kind(gateway)} discovery found {len(sp_found)} entries, over discovery.limits.{field}={limit}"
        )

    picks = _admit(selected, gateway.user_catalog, seen, dropped)
    if len(picks) > _count_limit(gateway.user_catalog)[1]:
        dropped.extend(Dropped(pick.fqn, pick.origin, "over_limit") for pick in picks)
        picks = []

    return Resolution(mounted=(*mounted, *sp_found, *picks), dropped=tuple(dropped))


def resolve_request(
    gateway: GatewayRuntime,
    sp_ws: WorkspaceClient,
    selections: Selections,
) -> Resolution:
    """Resolve from all chat-time sources; a limit error keeps pinned only."""
    return resolve_within_limits(
        gateway,
        pinned=PinnedSource(gateway).candidates(),
        discovered=SpDiscoverySource(gateway, sp_ws).candidates(),
        selected=SelectedSource(gateway, selections).candidates(),
    )


def resolve_within_limits(
    gateway: GatewayRuntime,
    *,
    pinned: Sequence[Candidate],
    discovered: Sequence[Candidate] = (),
    selected: Sequence[Candidate] = (),
) -> Resolution:
    """:func:`resolve`, falling back to pinned and selected when discovery is over its limit."""
    try:
        return resolve(gateway, pinned=pinned, discovered=discovered, selected=selected)
    except GatewayLimitError as exc:
        logger.error("[gateway] %s; using pinned entries and selections only", exc)
        return resolve(gateway, pinned=pinned, selected=selected)
