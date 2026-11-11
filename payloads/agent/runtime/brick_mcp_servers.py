"""Build the agent's MCP servers from ``bricks/mcp/`` state and the Unity AI gateway."""

from __future__ import annotations

import dataclasses
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from databricks.sdk import WorkspaceClient
from databricks_mcp.oauth_provider import DatabricksOAuthClientProvider

from agent_foundry import env
from agent_foundry._instance_layout import resolve_payload_subdir
from payloads.agent.runtime.gateway_selection import Selections
from payloads.agent.runtime.instance_paths import resolve_instance_dir
from payloads.agent.runtime.mcp_availability import McpBuild, UnavailableServer
from payloads.agent.runtime.mcp_tools_cache import identity_scope
from payloads.agent.runtime.request_context import get_obo_token, get_user_workspace_client, resolve_tools_client

if TYPE_CHECKING:
    # Pulled in only for type annotations on ``_mcp_servers_*``
    # helpers; the runtime import lives inside the function bodies
    # below to keep the existing import-cycle profile unchanged.
    from payloads.agent.runtime.mcp_wrapper import FilteredMCPServerStreamableHttp

logger = logging.getLogger(__name__)


_MCP_CLIENT_SESSION_TIMEOUT_SECONDS_DEFAULT = 180


def mcp_client_session_timeout_seconds() -> float:
    """Resolve the MCP ClientSession read-timeout in seconds.

    Reads ``MCP_CLIENT_SESSION_TIMEOUT_SECONDS`` (positive number);
    falls back to ``_MCP_CLIENT_SESSION_TIMEOUT_SECONDS_DEFAULT``.
    The OpenAI Agents SDK's default of 5s is too aggressive for UC
    function calls routed via Databricks Managed MCP, which may hit a
    cold-starting SQL warehouse and take tens of seconds to return.
    """
    raw = env.read_optional(env.MCP_CLIENT_SESSION_TIMEOUT_SECONDS) or ""
    try:
        value = float(raw)
    except ValueError:
        return _MCP_CLIENT_SESSION_TIMEOUT_SECONDS_DEFAULT
    return value if value > 0 else _MCP_CLIENT_SESSION_TIMEOUT_SECONDS_DEFAULT


def _make_mcp_meta_resolver(default_meta: dict[str, Any]) -> Any | None:
    """Return a ``tool_meta_resolver`` callable, or ``None`` when empty.

    The resolver returns a fresh dict on every invocation so the SDK's
    ``MCPUtil._merge_mcp_meta`` can mutate it without cross-call state.
    Snapshotting at construction time keeps the resolver pure.
    """
    if not default_meta:
        return None
    snapshot = dict(default_meta)

    def _resolver(_ctx: Any) -> dict[str, Any]:
        return dict(snapshot)

    return _resolver


def _normalise_allow_entry(item: Any) -> Any:
    """Coerce one ``tools.allow`` element to ``ToolAllowEntry``.

    The state file may carry either the legacy bare-string form
    (``allow: [foo, bar]``, equivalent to exact-match no-rename) or
    the post-aliasing dict form (``allow: [{match: foo*, expose_as:
    foo}]``).  Both shapes round-trip into the same wrapper input by
    funnelling through :class:`ToolAllowEntry`.

    Imported lazily inside the function body so this module's import
    surface stays unchanged for callers that don't touch MCP.
    """
    from agent_foundry.contracts.mcp import ToolAllowEntry

    if isinstance(item, str):
        return ToolAllowEntry(match=item)
    if isinstance(item, ToolAllowEntry):
        return item
    if isinstance(item, dict):
        return ToolAllowEntry.model_validate(item)
    raise TypeError(
        f"tools.allow[*] must be str or mapping; got {type(item).__name__}",
    )


@dataclasses.dataclass(frozen=True)
class _BrickServerSpec:
    """Parsed brick-state for one MCP server (immutable, cacheable).

    Holds every field needed to construct a
    :class:`~payloads.agent.runtime.mcp_wrapper.FilteredMCPServerStreamableHttp`
    EXCEPT the per-request ``auth_ws`` -- the OAuth provider closes
    over the workspace client and must therefore be reconstructed on
    every request to honour the OBO contract (different user -> fresh
    token).  Everything else is deploy-time state and stable for the
    process lifetime, so we cache the list of these specs once.

    The dataclass is frozen so cached instances cannot be mutated
    after creation; ``allow_entries`` is a tuple to keep the type
    hashable and consistent with the frozen contract.
    """

    sub_name: str
    url: str
    name_prefix: str
    allow_entries: tuple[Any, ...]
    prompts_cfg: Any
    resources_cfg: Any
    auto_poll_cfg: Any | None
    default_meta: dict[str, Any]


BRICK_SERVER_NAME_PREFIX = "mcp-brick-"


@dataclasses.dataclass(frozen=True)
class _BrickSpecLoad:
    """Parsed brick specs plus the servers skipped for invalid state."""

    specs: tuple[_BrickServerSpec, ...] = ()
    skipped: tuple[UnavailableServer, ...] = ()


def _load_brick_specs(
    instance_dir: Path,
    target: str,
    sp_ws: WorkspaceClient,
) -> list[_BrickServerSpec]:
    """The parsed specs of :func:`_load_brick_spec_status`, without the skips."""
    return list(_load_brick_spec_status(instance_dir, target, sp_ws).specs)


def _load_brick_spec_status(
    instance_dir: Path,
    target: str,
    sp_ws: WorkspaceClient,
) -> _BrickSpecLoad:
    """Read brick state for every MCP server and parse into specs.

    State is loaded via :func:`agent_foundry.state.load_state` from the
    workspace path (``/Shared/agent-foundry/<instance>/_state/
    mcp_servers/<sub>/<target>.json``) -- mirroring
    :class:`~agent_foundry.bricks.StandardLayoutBrick`.

    Silent on missing ``mcp_servers/`` directory or empty state -- this
    is the expected state for instances that have not adopted the MCP
    brick.  Per-server failures (missing URL, unresolved variables,
    duplicate brick subdir names, invalid allow / prompts / resources /
    auto_poll blocks) log a WARNING and skip that server, matching the
    previous behaviour.  Skips caused by a broken state file (unreadable
    state, unresolved variables, invalid allow / prompts / resources)
    are also returned in ``skipped`` so the request can report them.

    Note: two brick subdirs may legitimately share the same upstream
    URL (e.g. two ``managed_vector_search`` bricks pointing at indexes
    in the same ``{catalog}/{schema}``, since that URL is scoped above
    the index level).  Dedup therefore keys on ``sub.name`` -- the
    brick's directory name, which is the unique identifier for a
    server registration -- and not on the URL.

    This is the I/O-heavy step (one workspace API read per server) and
    is cached at process scope by :func:`build_mcp_servers`.
    """
    mcp_dir = resolve_payload_subdir(instance_dir, "mcp_servers")
    if not mcp_dir.is_dir():
        return _BrickSpecLoad()

    from agent_foundry.contracts.mcp import (
        AutoPollConfig,
        PromptsConfig,
        ResourcesConfig,
    )
    from agent_foundry.state import load_state as _ws_load

    instance_name = instance_dir.name
    specs: list[_BrickServerSpec] = []
    skipped: list[UnavailableServer] = []
    seen_subs: set[str] = set()

    for sub in sorted(mcp_dir.iterdir()):
        if not sub.is_dir() or sub.name.startswith(("_", ".")):
            continue
        if sub.name in seen_subs:
            logger.warning(
                "[mcp] duplicate brick subdir %r -- keeping first",
                sub.name,
            )
            continue

        try:
            state = (
                _ws_load(
                    sp_ws,
                    instance_name,
                    "mcp_servers",
                    sub.name,
                    target,
                )
                or {}
            )
        except Exception as exc:
            logger.warning(
                "[mcp] failed to load brick state for %r: %s",
                sub.name,
                exc,
            )
            skipped.append(UnavailableServer(sub.name, "invalid_state"))
            continue

        url = state.get("url", "")
        if not url:
            logger.debug(
                "[mcp] no state for brick server %r at target %r -- skipping",
                sub.name,
                target,
            )
            continue
        if "${var" in url:
            logger.warning(
                "[mcp] unresolved variables in brick MCP URL %s -- skipping",
                url,
            )
            skipped.append(UnavailableServer(sub.name, "invalid_state"))
            continue

        tools_cfg = state.get("tools", {})
        raw_allow = list(tools_cfg.get("allow", []))
        if not raw_allow:
            logger.warning(
                "[mcp] empty tools.allow for brick server %r -- skipping "
                "(state file may be malformed; redeploy the brick)",
                sub.name,
            )
            skipped.append(UnavailableServer(sub.name, "invalid_state"))
            continue

        # Normalise the allow list to ``ToolAllowEntry`` objects so the
        # wrapper has a single shape to work with, regardless of
        # whether the state file used the legacy bare-string form
        # (``allow: [foo, bar]``) or the post-aliasing dict form
        # (``allow: [{match: foo, expose_as: bar}]``).
        try:
            allow_entries = tuple(_normalise_allow_entry(item) for item in raw_allow)
        except Exception as exc:
            logger.warning(
                "[mcp] invalid tools.allow for brick server %r: %s -- "
                "skipping (state file may be stale; redeploy the brick)",
                sub.name,
                exc,
            )
            skipped.append(UnavailableServer(sub.name, "invalid_state"))
            continue

        # Prompts / resources are re-hydrated through their PR 1
        # contracts so a hand-edited state file with a bad ``cache:``
        # value is caught here rather than failing mid-request.
        # Defaults are ``enabled=False`` -- a server with no opt-in
        # stays inert at runtime.
        try:
            prompts_cfg = PromptsConfig.model_validate(
                state.get("prompts", {}),
            )
            resources_cfg = ResourcesConfig.model_validate(
                state.get("resources", {}),
            )
        except Exception as exc:
            logger.warning(
                "[mcp] invalid prompts/resources for brick server %r: %s -- "
                "skipping (state file may be stale; redeploy the brick)",
                sub.name,
                exc,
            )
            skipped.append(UnavailableServer(sub.name, "invalid_state"))
            continue

        auto_poll_cfg: AutoPollConfig | None = None
        if "auto_poll" in state:
            try:
                auto_poll_cfg = AutoPollConfig.model_validate(state["auto_poll"])
            except Exception as exc:
                logger.warning(
                    "[mcp] invalid auto_poll for brick server %r: %s -- "
                    "disabling auto-poll for this server (state file may "
                    "be stale; redeploy the brick)",
                    sub.name,
                    exc,
                )

        specs.append(
            _BrickServerSpec(
                sub_name=sub.name,
                url=url,
                name_prefix=state.get("name_prefix", ""),
                allow_entries=allow_entries,
                prompts_cfg=prompts_cfg,
                resources_cfg=resources_cfg,
                auto_poll_cfg=auto_poll_cfg,
                default_meta=dict(tools_cfg.get("default_meta", {})),
            )
        )
        seen_subs.add(sub.name)
        logger.info(
            "[mcp] registered brick MCP server %r -> %s (allow=%d, name_prefix=%r, auto_poll=%s)",
            sub.name,
            url,
            len(allow_entries),
            state.get("name_prefix", ""),
            f"{auto_poll_cfg.trigger}->{auto_poll_cfg.poll_with}" if auto_poll_cfg else "off",
        )

    return _BrickSpecLoad(specs=tuple(specs), skipped=tuple(skipped))


def _mcp_servers_from_specs(
    specs: list[_BrickServerSpec],
    auth_ws: WorkspaceClient,
    cache_scope: str = "",
) -> list[FilteredMCPServerStreamableHttp]:
    """Construct ``FilteredMCPServerStreamableHttp`` per spec.

    Pure in-memory step: no I/O.  Builds a fresh
    ``DatabricksOAuthClientProvider(auth_ws)`` per server so the
    OAuth provider closes over the request's OBO token (or the
    service principal client when no OBO is present).  *cache_scope*
    must identify that same caller; empty disables the tools-list cache.
    """
    from payloads.agent.runtime.mcp_wrapper import FilteredMCPServerStreamableHttp

    servers: list[FilteredMCPServerStreamableHttp] = []
    for spec in specs:
        try:
            server = FilteredMCPServerStreamableHttp(
                params={
                    "url": spec.url,
                    "auth": DatabricksOAuthClientProvider(auth_ws),
                },
                name_prefix=spec.name_prefix,
                allow_entries=list(spec.allow_entries),
                prompts=spec.prompts_cfg,
                resources=spec.resources_cfg,
                auto_poll=spec.auto_poll_cfg,
                server_url=spec.url,
                cache_scope=cache_scope,
                tool_meta_resolver=_make_mcp_meta_resolver(spec.default_meta),
                cache_tools_list=True,
                name=f"{BRICK_SERVER_NAME_PREFIX}{spec.sub_name}",
                client_session_timeout_seconds=mcp_client_session_timeout_seconds(),
            )
        except Exception as exc:
            logger.warning(
                "[mcp] failed to create brick MCP server %r: %s",
                spec.sub_name,
                exc,
            )
            continue
        servers.append(server)
    return servers


_BRICK_SPECS_CACHE: dict[tuple[Path, str], _BrickSpecLoad] = {}


def _clear_brick_specs_cache() -> None:
    """Test-only escape hatch.  Production code must never call this."""
    _BRICK_SPECS_CACHE.clear()


def build_mcp_servers(
    config: dict[str, Any],
    sp_ws: WorkspaceClient,
    *,
    selections: Selections | None = None,
) -> list[FilteredMCPServerStreamableHttp]:
    """The servers of :func:`build_mcp_servers_with_status`, without the unavailable ones."""
    return build_mcp_servers_with_status(config, sp_ws, selections=selections).servers


def build_mcp_servers_with_status(
    config: dict[str, Any],
    sp_ws: WorkspaceClient,
    *,
    selections: Selections | None = None,
) -> McpBuild:
    """Build the workspace MCP servers, then the Unity Gateway MCP services.

    Workspace servers come from the ``bricks/mcp/`` brick state that the
    ``mcp_servers/*/server.yml`` files deploy (PR 5 / #111).  Per-server
    config files written by the brick to the workspace carry explicit
    ``tools.allow``, ``name_prefix``, ``default_meta``, ``prompts`` and
    ``resources``, all wired into
    :class:`~payloads.agent.runtime.mcp_wrapper.FilteredMCPServerStreamableHttp`.

    With a ``tools.mcp.gateway`` block the resolved gateway services --
    pinned, discovered and the request's *selections* -- follow them (see
    :mod:`payloads.agent.runtime.gateway_mcp`).  Workspace servers come
    first so they claim their tool names before any gateway service.

    Returned servers are unconnected; the caller (typically
    :func:`payloads.agent.runtime.mcp_connect.managed_mcp_servers`) owns
    the connect/cleanup lifecycle.  ``unavailable`` lists the configured
    servers this request cannot mount.

    Process-level memoization
    --------------------------
    The deploy-time spec list (``_BrickServerSpec``) is cached by
    ``(instance_dir, target)`` for the lifetime of the process.
    Production trace showed the underlying workspace state reads
    (~100ms each, sequential, one per brick MCP server) costing
    ~0.4s on every request -- now amortised to a single cold read.

    The OAuth provider is NOT cached: it closes over the request's user
    ``WorkspaceClient`` (see :func:`~payloads.agent.runtime.request_context.resolve_tools_client`).  Wrapper
    objects are therefore reconstructed on every call via
    :func:`_mcp_servers_from_specs`, which is a cheap in-memory step.
    """
    workspace = _workspace_mcp_build(sp_ws)
    gateway = config.get("GATEWAY_MCP")
    if gateway is None:
        return workspace
    from payloads.agent.runtime.gateway_mcp import build_gateway_mcp_servers_with_status

    gateway_build = build_gateway_mcp_servers_with_status(
        gateway,
        sp_ws,
        get_user_workspace_client(),
        selections=selections or Selections(),
        session_timeout_seconds=mcp_client_session_timeout_seconds(),
        user_cache_scope=identity_scope(get_obo_token()),
    )
    return McpBuild(
        servers=[*workspace.servers, *gateway_build.servers],
        unavailable=(*workspace.unavailable, *gateway_build.unavailable),
    )


def _workspace_mcp_build(sp_ws: WorkspaceClient) -> McpBuild:
    load = _cached_brick_load(sp_ws)
    if not load.specs:
        return McpBuild(servers=[], unavailable=load.skipped)
    servers = _mcp_servers_from_specs(
        list(load.specs),
        resolve_tools_client(sp_ws),
        cache_scope=identity_scope(get_obo_token()),
    )
    return McpBuild(servers=servers, unavailable=load.skipped)


def _cached_brick_load(sp_ws: WorkspaceClient) -> _BrickSpecLoad:
    instance_dir = resolve_instance_dir()
    if instance_dir is None:
        return _BrickSpecLoad()
    target = env.read(env.DEPLOY_TARGET)
    cache_key = (instance_dir, target)
    load = _BRICK_SPECS_CACHE.get(cache_key)
    if load is None:
        load = _load_brick_spec_status(instance_dir, target, sp_ws)
        _BRICK_SPECS_CACHE[cache_key] = load
    return load


def _cached_brick_specs(sp_ws: WorkspaceClient) -> list[_BrickServerSpec]:
    return list(_cached_brick_load(sp_ws).specs)


def workspace_mcp_server_names(sp_ws: WorkspaceClient) -> list[str]:
    """Names of the ``server.yml`` MCP servers the agent mounts."""
    return [spec.sub_name for spec in _cached_brick_specs(sp_ws)]
