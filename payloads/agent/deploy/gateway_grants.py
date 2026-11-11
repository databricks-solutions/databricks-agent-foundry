"""Grant the App's service principal the Unity Gateway resources it runs as.

App ``uc_securable`` resources cover model services only, so MCP services
and skills used with ``identity: sp`` are granted through the Unity Catalog
grants API once the App -- and with it the service principal -- exists:
``EXECUTE`` on pinned MCP services, ``READ_VOLUME`` on pinned skills and on
each skills discovery schema.  Resources pinned ``obo`` run as the
signed-in user and are never granted to the SP.  Resources in the
``system`` catalog are platform-managed: deployers cannot grant on them and
workspace principals can already use them.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from databricks.sdk.errors.base import DatabricksError
from databricks.sdk.service.catalog import PermissionsChange, Privilege

if TYPE_CHECKING:
    from databricks.sdk import WorkspaceClient

    from payloads.agent.contracts import GatewayMcpRuntime, GatewaySkillsRuntime

GATEWAY_SP_MCP_SERVICES_VAR = "gateway_sp_mcp_services"
GATEWAY_SP_SKILLS_VAR = "gateway_sp_skills"
GATEWAY_SP_SKILL_SCHEMAS_VAR = "gateway_sp_skill_schemas"
_PLATFORM_CATALOG = "system"
_SDK_CONFIG_SUFFIX = ". Config: "


@dataclass(frozen=True)
class GatewayGrant:
    """One privilege on one securable, for the App's service principal."""

    securable_type: str
    full_name: str
    privilege: Privilege


def sp_pinned_fqns(gateway: GatewayMcpRuntime | GatewaySkillsRuntime | None) -> list[str]:
    """Sorted FQNs of the resources pinned with ``identity: sp``."""
    if gateway is None:
        return []
    return sorted(pinned.fqn for pinned in gateway.pinned if pinned.identity == "sp")


def sp_discovery_schemas(gateway: GatewaySkillsRuntime | None) -> list[str]:
    """Sorted schemas the service principal discovers skills in."""
    if gateway is None or not gateway.discovery.enabled:
        return []
    return sorted(set(gateway.discovery.locations))


def plan_gateway_grants(
    mcp_services: Iterable[str],
    *,
    skills: Iterable[str] = (),
    skill_schemas: Iterable[str] = (),
) -> list[GatewayGrant]:
    """``USE CATALOG`` / ``USE SCHEMA`` on each parent, then the leaf privileges.

    ``EXECUTE`` on each MCP service, ``READ_VOLUME`` on each skill and on
    each skills schema (inherited by every skill in it).
    """
    services = sorted(set(mcp_services))
    skill_fqns = sorted(set(skills))
    schemas_read = sorted(set(skill_schemas))
    schemas = sorted({fqn.rsplit(".", 1)[0] for fqn in (*services, *skill_fqns)} | set(schemas_read))
    catalogs = sorted({fqn.split(".", 1)[0] for fqn in schemas})
    return [
        *(GatewayGrant("catalog", name, Privilege.USE_CATALOG) for name in catalogs),
        *(GatewayGrant("schema", name, Privilege.USE_SCHEMA) for name in schemas),
        *(GatewayGrant("mcp_service", name, Privilege.EXECUTE) for name in services),
        *(GatewayGrant("skill", name, Privilege.READ_VOLUME) for name in skill_fqns),
        *(GatewayGrant("schema", name, Privilege.READ_VOLUME) for name in schemas_read),
    ]


def _is_platform_managed(fqn: str) -> bool:
    """True for resources in the ``system`` catalog."""
    return fqn.split(".", 1)[0] == _PLATFORM_CATALOG


def _grantable(fqns: list[str]) -> list[str]:
    return [fqn for fqn in fqns if not _is_platform_managed(fqn)]


def _error_summary(exc: DatabricksError) -> str:
    """The API error without the SDK's appended client-config dump."""
    return str(exc).split(_SDK_CONFIG_SUFFIX, 1)[0]


def _apply_grant(ws: WorkspaceClient, principal: str, grant: GatewayGrant) -> None:
    label = f"{grant.privilege.value} on {grant.securable_type} {grant.full_name}"
    try:
        ws.grants.update(
            securable_type=grant.securable_type,
            full_name=grant.full_name,
            changes=[PermissionsChange(principal=principal, add=[grant.privilege])],
        )
    except DatabricksError as exc:
        print(f"  WARNING: could not grant {label} to app SP: {_error_summary(exc)}")
        return
    print(f"  Granted {label} to app SP")


def ensure_app_sp_gateway_grants(
    ws: WorkspaceClient | None,
    app_name: str,
    mcp_services: Iterable[str],
    *,
    skills: Iterable[str] = (),
    skill_schemas: Iterable[str] = (),
    dry_run: bool = False,
) -> None:
    """Grant the App SP what its ``sp`` gateway resources need.

    Idempotent (``grants.update`` adds privileges already held as a
    no-op) and best-effort: a denied or failed grant is reported and
    skipped so the deploy carries on.
    """
    services, skill_fqns, schemas = sorted(set(mcp_services)), sorted(set(skills)), sorted(set(skill_schemas))
    platform = [fqn for fqn in (*services, *skill_fqns, *schemas) if _is_platform_managed(fqn)]
    if platform:
        print(f"  Skipping grants for platform-managed Unity Gateway resources: {', '.join(platform)}")
    grants = plan_gateway_grants(
        _grantable(services),
        skills=_grantable(skill_fqns),
        skill_schemas=_grantable(schemas),
    )
    if not grants:
        return
    if dry_run:
        for grant in grants:
            print(f"  [DRY RUN] Would grant {grant.privilege.value} on {grant.securable_type} {grant.full_name}")
        return
    if ws is None:
        return

    from payloads.agent.deploy.lakebase import _get_app_sp_client_id

    principal = _get_app_sp_client_id(ws, app_name)
    if not principal:
        print("  WARNING: App has no service_principal_client_id; skipping Unity Gateway grants")
        return
    for grant in grants:
        _apply_grant(ws, principal, grant)


def split_fqns(raw: str) -> list[str]:
    """Parse the comma-joined FQN list published in the variable bag."""
    return [fqn for fqn in raw.split(",") if fqn]
