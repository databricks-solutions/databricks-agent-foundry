"""``agent-foundry brick mcp resolve`` -- show what a gateway-mode agent mounts.

Prints the effective policy from ``spec.runtime.tools.mcp.gateway`` and
the resolved set, using the same resolution code as the runtime:

* ``--as sp`` (default): pinned services plus what discovery finds.  The
  listing runs with the CLI's credentials, standing in for the app's
  service principal -- use the deployer's service principal profile for
  an exact view.
* ``--as-current-user``: additionally lists the ``user_catalog``
  locations as you, showing which services you could select.  Needs user
  credentials; a service principal profile is refused.

Tool-level rules (name collisions, ``max_tools``) need live connections
and are only applied at chat time.
"""

from __future__ import annotations

import argparse
import sys
from typing import TYPE_CHECKING

from databricks.sdk.errors.base import DatabricksError

from agent_foundry.variables import add_project_root_arg
from payloads.agent.runtime.gateway_resolve import (
    Candidate,
    PinnedSource,
    Resolution,
    SpDiscoverySource,
    config_hash,
    list_mcp_service_fqns,
    resolve,
)

if TYPE_CHECKING:
    from databricks.sdk import WorkspaceClient

    from payloads.agent.contracts import GatewayMcpRuntime, McpDiscoverySpec

# SDK auth types that authenticate as a service principal, not a user.
_SERVICE_PRINCIPAL_AUTH_TYPES = frozenset(
    {"oauth-m2m", "azure-client-secret", "github-oidc", "azure-devops-oidc", "env-oidc", "file-oidc"}
)


class UserCredentialsRequired(RuntimeError):
    """``--as-current-user`` with credentials that are not a user's."""


def _describe_block(label: str, spec: McpDiscoverySpec) -> list[str]:
    if not spec.enabled:
        return [f"  {label}: disabled"]
    lines = [f"  {label}: {', '.join(spec.locations)} (max_servers={spec.limits.max_servers})"]
    for name, values in (("include", spec.include), ("exclude", spec.exclude), ("exclude_tools", spec.exclude_tools)):
        if values:
            lines.append(f"    {name}: {', '.join(values)}")
    return lines


def _describe_candidate(index: int, candidate: Candidate) -> str:
    line = f"    {index}. {candidate.fqn}  identity={candidate.identity}  origin={candidate.origin}"
    if candidate.allow:
        line += f"  allow={','.join(candidate.allow)}"
    return line


def render(gateway: GatewayMcpRuntime, resolution: Resolution, *, as_label: str) -> str:
    """Human-readable policy and resolution; names only, never credentials."""
    lines = [f"Unity Gateway MCP resolution (as {as_label}), config_hash={config_hash(gateway)}", "Policy:"]
    lines.append(f"  pinned: {', '.join(p.fqn for p in gateway.pinned) or '(none)'}")
    lines += _describe_block("discovery", gateway.discovery)
    lines += _describe_block("user_catalog", gateway.user_catalog)
    lines.append("Mounted, in order:")
    lines += [_describe_candidate(i, c) for i, c in enumerate(resolution.mounted, start=1)] or ["    (none)"]
    if resolution.dropped:
        lines.append("Dropped:")
        lines += [f"    - {d.fqn}  origin={d.origin}  reason={d.reason}" for d in resolution.dropped]
    return "\n".join(lines)


def _catalog_candidates(gateway: GatewayMcpRuntime, ws: WorkspaceClient) -> list[Candidate]:
    if ws.config.auth_type in _SERVICE_PRINCIPAL_AUTH_TYPES:
        raise UserCredentialsRequired(
            f"--as-current-user needs user credentials; this profile authenticates as a "
            f"service principal (auth_type={ws.config.auth_type})"
        )
    spec = gateway.user_catalog
    if not spec.enabled:
        return []
    exclude_tools = tuple(spec.exclude_tools)
    fqns = list_mcp_service_fqns(ws, spec.locations)
    return [Candidate(fqn, "obo", "selected", exclude_tools=exclude_tools) for fqn in fqns]


def resolve_for_cli(gateway: GatewayMcpRuntime, ws: WorkspaceClient, *, as_current_user: bool) -> Resolution:
    """Pinned + discovered, plus the user's catalog with ``as_current_user``."""
    selected = _catalog_candidates(gateway, ws) if as_current_user else []
    return resolve(
        gateway,
        pinned=PinnedSource(gateway).candidates(),
        discovered=SpDiscoverySource(gateway, ws).candidates(),
        selected=selected,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Show the Unity Gateway MCP services an agent instance resolves to.")
    parser.add_argument("--instance", required=True, help="Instance name (resolved under instances/<name>/).")
    parser.add_argument("--target", default="dev")
    parser.add_argument("--profile", default=None, help="Databricks CLI profile (default: SDK credential resolution).")
    who = parser.add_mutually_exclusive_group()
    who.add_argument("--as", dest="as_identity", choices=["sp"], default="sp", help="Resolve as the service principal.")
    who.add_argument("--as-current-user", action="store_true", help="Also list the user catalog as you.")
    add_project_root_arg(parser)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    from agent_foundry.workspace_client import prefer_workspace_client
    from payloads.agent.config import resolve_agent_spec

    gateway = resolve_agent_spec(args.project_root / "instances" / args.instance).runtime.tools.mcp.gateway
    if gateway is None:
        print("spec.runtime.tools.mcp.gateway is not set; nothing to resolve.", file=sys.stderr)
        return 1

    ws = prefer_workspace_client(profile=args.profile)
    if ws is None:
        print("Error: no Databricks workspace client available.", file=sys.stderr)
        return 1
    try:
        resolution = resolve_for_cli(gateway, ws, as_current_user=args.as_current_user)
    except (UserCredentialsRequired, DatabricksError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(render(gateway, resolution, as_label="current user" if args.as_current_user else "sp"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
