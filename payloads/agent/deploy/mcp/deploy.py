"""
MCP brick deploy script (issue #107)
=====================================

Deploys one ``server.yml`` to a per-target state file in the workspace.
URL construction is fully deterministic from the server type plus a
small set of fields, so the only SDK call is the workspace ``files/``
write executed by :func:`agent_foundry.state.save_state`.
Network-touching inspection lives in the ``agent-foundry brick mcp
inspect`` CLI instead.

Persistence: state is written to
``/Shared/agent-foundry/<instance>/_state/mcp_servers/<sub>/<target>.json``
via :func:`agent_foundry.state.save_state`.  No local ``_state/``
directory is created -- the workspace is the single source of truth
(see issue #50: stale per-fork local files must not override live
state at deploy time, and they must not be bundled into deployed app
source via ``databricks sync``).

Runtime in PR 2 (#108) reads this state via
``MCPBrick.load_state_outputs`` and ``build_mcp_servers``, both of
which call :func:`agent_foundry.state.load_state` (workspace-only).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from agent_foundry._instance_layout import resolve_payload_subdir
from agent_foundry.contracts.mcp import (
    CustomAppServerConfig,
    ExternalServerConfig,
    ManagedFunctionsServerConfig,
    ManagedGenieServerConfig,
    ManagedSqlServerConfig,
    ManagedVectorSearchServerConfig,
    ServerConfig,
)
from agent_foundry.state import save_state
from payloads.agent.deploy.mcp.validate import validate_server_yml


def _get_client(
    variables: dict[str, str] | None = None,
    *,
    ws: Any | None = None,
    construct: bool = False,
) -> Any | None:
    if ws is not None:
        return ws
    if not construct:
        return None
    from agent_foundry.workspace_client import prefer_workspace_client

    return prefer_workspace_client(variables)


def build_canonical_url(config: ServerConfig, host: str) -> str:
    """Map a typed ``ServerConfig`` to its canonical MCP URL.

    Mirrors the patterns documented in ``docs/mcp-field-guide.md``::

        managed_functions      {host}/api/2.0/mcp/functions/{catalog}/{schema}
        managed_genie          {host}/api/2.0/mcp/genie/{space_id}
        managed_vector_search  {host}/api/2.0/mcp/vector-search/{catalog}/{schema}
        managed_sql            {host}/api/2.0/mcp/sql
        external               {host}/api/2.0/mcp/external/{connection_name}
        custom_app             {app_url}   (full URL, host arg ignored)

    ``host`` is required for every type *except* ``custom_app``; an
    empty host raises ``ValueError`` so the deploy fails loudly rather
    than emitting a malformed URL.
    """
    # custom_app is the only type that doesn't depend on the workspace host.
    if isinstance(config, CustomAppServerConfig):
        return config.app_url.rstrip("/")

    host_clean = (host or "").rstrip("/")
    if not host_clean:
        raise ValueError(
            f"workspace host required for type {config.type!r} -- "
            "set 'host' under [targets.<target>] in foundry.toml or pass --vars host=..."
        )

    if isinstance(config, ManagedFunctionsServerConfig):
        return f"{host_clean}/api/2.0/mcp/functions/{config.catalog}/{config.schema_}"
    if isinstance(config, ManagedGenieServerConfig):
        return f"{host_clean}/api/2.0/mcp/genie/{config.space_id}"
    if isinstance(config, ManagedVectorSearchServerConfig):
        return f"{host_clean}/api/2.0/mcp/vector-search/{config.catalog}/{config.schema_}"
    if isinstance(config, ManagedSqlServerConfig):
        return f"{host_clean}/api/2.0/mcp/sql"
    if isinstance(config, ExternalServerConfig):
        return f"{host_clean}/api/2.0/mcp/external/{config.connection_name}"

    # Defensive fallback: a new server type was added to the discriminated
    # union but not to this dispatch. Surface it explicitly rather than
    # silently returning an empty string.
    raise NotImplementedError(f"build_canonical_url: no URL pattern for type {config.type!r}")


def _resolve_name_prefix(config: ServerConfig, server_name: str) -> str:
    """Return the explicit ``name_prefix`` or derive ``<server_name>__``."""
    if config.tools.name_prefix is not None:
        return config.tools.name_prefix
    return f"{server_name}__"


def _build_state(
    config: ServerConfig,
    url: str,
    server_name: str,
) -> dict[str, Any]:
    """Serialise the deploy outputs into a JSON-friendly state dict.

    ``auto_poll`` is dumped only when set (the post-validation
    resolved value is ``AutoPollConfig | None``).  Persisting it as
    ``None`` would force the runtime to handle an extra absent-vs-null
    case; we keep absence to mean "off" instead.
    """
    state: dict[str, Any] = {
        "url": url,
        "type": config.type,
        "name_prefix": _resolve_name_prefix(config, server_name),
        "tools": config.tools.model_dump(by_alias=True),
        "prompts": config.prompts.model_dump(by_alias=True),
        "resources": config.resources.model_dump(by_alias=True),
    }
    # ``resolved_auto_poll`` narrows the field's tri-state YAML shape
    # (``AutoPollConfig | bool | None``) to its post-validator value
    # (``AutoPollConfig | None``).
    auto_poll = config.resolved_auto_poll
    if auto_poll is not None:
        state["auto_poll"] = auto_poll.model_dump(by_alias=True)
    return state


def _workspace_state_path(
    instance_name: str,
    server_name: str,
    target: str,
) -> str:
    """Return the canonical workspace path used by :func:`save_state`.

    Pure formatting helper -- mirrors the path built by
    ``agent_foundry.state._ws_state_path`` so we can log a useful
    location to the deployer without importing a private symbol.  If
    that internal layout ever changes, both this helper and the state
    module need to update together (caught by ``test_deploy``).
    """
    return f"/Shared/agent-foundry/{instance_name}/_state/mcp_servers/{server_name}/{target}.json"


def deploy(
    sub_dir: Path,
    target: str,
    variables: dict[str, str],
    dry_run: bool = False,
    *,
    ws: Any | None = None,
) -> int:
    """Deploy one MCP server sub-instance.

    Persists state to the workspace (single source of truth, issue
    #50) via :func:`agent_foundry.state.save_state`.  No local
    ``_state/`` directory is ever created.

    Returns 0 on success, 1 on validation error or unresolved variables.
    """
    server_name = sub_dir.name
    yml_path = sub_dir / "server.yml"

    config, errors, warnings = validate_server_yml(yml_path, variables)
    for w in warnings:
        print(f"  [{server_name}] WARNING: {w}")
    if config is None:
        for e in errors:
            print(f"  [{server_name}] ERROR: {e}", file=sys.stderr)
        return 1

    try:
        url = build_canonical_url(config, variables.get("host", ""))
    except ValueError as exc:
        print(f"  [{server_name}] ERROR: {exc}", file=sys.stderr)
        return 1

    state = _build_state(config, url, server_name)

    if dry_run:
        print(f"  [{server_name}] [DRY RUN] type={config.type}")
        print(f"  [{server_name}] [DRY RUN] url={url}")
        print(f"  [{server_name}] [DRY RUN] tools.allow={config.tools.allow}")
        print(
            f"  [{server_name}] [DRY RUN] prompts.enabled={config.prompts.enabled}, "
            f"resources.enabled={config.resources.enabled}"
        )
        return 0

    # Workspace-only persistence (issue #50): no local ``_state/``
    # file is ever written.  Stale per-fork local copies would
    # otherwise override the live state at deploy time and get
    # bundled into deployed app source by ``databricks sync``.
    # ``sub_dir`` is ``<instance>/payloads/<payload>/mcp_servers/<server>``;
    # the workspace state key is the instance dir name (must match the
    # runtime read in ``payloads/agent/runtime/brick_mcp_servers.py``, which uses
    # ``instance_dir.name``).  parents: [0]=mcp_servers, [1]=<payload>,
    # [2]=payloads, [3]=<instance>.
    instance_name = sub_dir.parents[3].name
    deployer = variables.get("deployer", "")
    ws = _get_client(variables, ws=ws)
    save_state(
        ws,
        instance_name,
        "mcp_servers",
        server_name,
        target,
        state,
        action="deploy",
        deployer=deployer,
        resource_id=url,
    )
    ws_path = _workspace_state_path(instance_name, server_name, target)
    print(f"  [{server_name}] type={config.type}")
    print(f"  [{server_name}] url={url}")
    print(f"  [{server_name}] state written to workspace: {ws_path}")
    return 0


# ---------------------------------------------------------------------------
# CLI -- iterates all servers under ``instances/<app>/mcp_servers/``
# ---------------------------------------------------------------------------
#
# The orchestrator path (``MCPBrick.deploy``) calls :func:`deploy` directly
# per sub-directory; this CLI is the standalone equivalent so deployers
# can iterate a single brick without running the full ``agent-foundry
# deploy`` sequence.


def _discover_server_dirs(mcp_dir: Path) -> list[Path]:
    """Return the sorted set of valid server sub-dirs under ``mcp_dir``."""
    return sorted(
        d for d in mcp_dir.iterdir() if d.is_dir() and not d.name.startswith(("_", ".")) and (d / "server.yml").exists()
    )


def main(argv: list[str] | None = None) -> int:
    from agent_foundry.bricks import fill_state_outputs, load_brick_variables
    from agent_foundry.variables import add_project_root_arg

    parser = argparse.ArgumentParser(
        description=(
            "Deploy MCP servers under instances/<instance>/mcp_servers/. "
            "Iterates every server.yml unless --server is given."
        ),
    )
    parser.add_argument(
        "--instance",
        required=True,
        help="Instance name (resolved under instances/<name>/).",
    )
    parser.add_argument(
        "--server",
        default=None,
        help="Optional: deploy only this server sub-directory.",
    )
    parser.add_argument("--target", default="dev")
    parser.add_argument(
        "--vars",
        nargs="*",
        default=[],
        help="Extra variables as key=value pairs.",
    )
    parser.add_argument("--dry-run", action="store_true")
    add_project_root_arg(parser)
    args = parser.parse_args(argv)

    project_root = args.project_root
    instance_dir = project_root / "instances" / args.instance
    if not instance_dir.exists():
        print(
            f"Error: instance directory not found: {instance_dir}",
            file=sys.stderr,
        )
        return 1

    mcp_dir = resolve_payload_subdir(instance_dir, "mcp_servers")
    if not mcp_dir.is_dir():
        print(f"  No mcp_servers/ directory under {instance_dir} -- nothing to deploy")
        return 0

    variables = load_brick_variables(
        project_root,
        args.target,
        instance_dir,
        extra_vars=args.vars,
    )

    sub_dirs = _discover_server_dirs(mcp_dir)
    if args.server is not None:
        sub_dirs = [d for d in sub_dirs if d.name == args.server]
        if not sub_dirs:
            print(
                f"Error: server {args.server!r} not found under {mcp_dir}",
                file=sys.stderr,
            )
            return 1

    if not sub_dirs:
        print(f"  No MCP servers found under {mcp_dir}")
        return 0

    print(f"  Deploying {len(sub_dirs)} MCP server(s) from {mcp_dir}")
    ws = _get_client(variables, construct=True)
    variables = fill_state_outputs(variables, instance_dir, args.target, ws)
    rc = 0
    for sub in sub_dirs:
        sub_rc = deploy(sub, args.target, variables, dry_run=args.dry_run, ws=ws)
        if sub_rc != 0:
            rc = sub_rc
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
