"""Deploy-time provisioning of the optional UC **MCP Service** governance layer.

This is **path C** (see the payload README): a Unity Catalog ``MCP_SERVICE``
securable built *on top of* an existing ``is_mcp`` HTTP connection to expose it
through the AI Gateway with governance -- tool selection, service policies, rate
limits and ``EXECUTE`` grants.  It is **opt-in per API** (an ``mcp_service:``
block on the API's spec) and **Beta** (gated behind the account-level Unity AI
Gateway + Managed MCP Servers previews).

Clean separation is the core requirement.  This module is **strictly layered on
top of** :mod:`payloads.rest_mcp.deploy.connection`:

* it *reads* the connection name (via the caller, which passes the map from
  :func:`~payloads.rest_mcp.deploy.connection.loaded_connections`) but never
  imports or mutates the connection module;
* it keeps its own workspace-state partition (``brick_type =
  "rest_mcp_service"``) so connection and service state never collide;
* agent consumption is unchanged -- it stays on the ``type: external``
  connection.  The service only *defines* the governance surface, which the
  platform enforces once consumption moves to the gateway URL.

All the wire shapes below were live-confirmed against the Beta
``/api/2.1/unity-catalog/mcp-services`` API (there is no published REST
reference yet).  The friendly config enums are mapped to the platform's
``RATE_LIMIT_*`` / ``POLICY_TYPE_*`` tokens here so that churn stays out of the
user-facing config contract.  Raw ``api_client.do`` is used because the typed
SDK does not model this Beta securable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from agent_foundry.state import delete_state, load_state, save_state

if TYPE_CHECKING:
    from payloads.rest_mcp.contracts import McpServiceSpec, RestMcpPayloadSpec

# Own state partition -- never shares ``rest_mcp`` (connections) so the two
# layers stay independent (one ``<sub_instance>`` == one API name).
MCP_SERVICE_BRICK_TYPE = "rest_mcp_service"

_SERVICES_API = "/api/2.1/unity-catalog/mcp-services"
_SCHEMAS_API = "/api/2.1/unity-catalog/schemas"
_PERMISSIONS_API = "/api/2.1/unity-catalog/permissions/mcp_service"

# Friendly config value -> platform Beta enum token (spike-confirmed).
_RATE_LIMIT_KEY = {
    "user": "RATE_LIMIT_KEY_USER",
    "user_group": "RATE_LIMIT_KEY_USER_GROUP",
    "service_principal": "RATE_LIMIT_KEY_SERVICE_PRINCIPAL",
}
_RENEWAL_PERIOD = {"minute": "RATE_LIMIT_RENEWAL_PERIOD_MINUTE"}
_POLICY_TYPE = {"builtin": "POLICY_TYPE_BUILTIN", "custom": "POLICY_TYPE_CUSTOM"}
# Substrings that mark a "the Beta preview is not enabled" failure, so deploy
# degrades to a clear warning instead of crashing (the connection is already
# provisioned and usable via ``type: external``).
_PREVIEW_OFF_MARKERS = ("preview", "not enabled", "not supported", "not available", "feature flag")


def mcp_service_full_name(catalog: str, schema: str, api_name: str) -> str:
    """3-level UC name of the service securable: ``<catalog>.<schema>.<api>``."""
    return f"{catalog}.{schema}.{api_name}"


def default_schema(schema_prefix: str) -> str:
    """Derived schema for proxy services: ``<schema_prefix>_rest_mcp``.

    Mirrors the agent payload's ``<schema_prefix>_agent`` convention so proxy
    artifacts land in a predictable, instance-owned schema.
    """
    return f"{schema_prefix}_rest_mcp"


def _resolve_target(svc: McpServiceSpec, *, catalog: str, schema_prefix: str) -> tuple[str, str]:
    """Resolve ``(catalog, schema)`` for a service, honouring per-API overrides."""
    return svc.catalog or catalog, svc.uc_schema or default_schema(schema_prefix)


# ---------------------------------------------------------------------------
# raw API helpers (SDK-thin; typed SDK does not model this Beta securable)
# ---------------------------------------------------------------------------


def _is_preview_off(exc: Exception) -> bool:
    return any(marker in str(exc).lower() for marker in _PREVIEW_OFF_MARKERS)


def _ensure_schema(client: Any, catalog: str, schema: str) -> None:
    """Create ``<catalog>.<schema>`` if it does not already exist."""
    from databricks.sdk.errors import NotFound

    try:
        client.api_client.do("GET", f"{_SCHEMAS_API}/{catalog}.{schema}")
        return
    except NotFound:
        pass
    client.api_client.do("POST", _SCHEMAS_API, body={"catalog_name": catalog, "name": schema})


def _get_service(client: Any, full_name: str) -> dict[str, Any] | None:
    from databricks.sdk.errors import NotFound

    try:
        result = client.api_client.do("GET", f"{_SERVICES_API}/{full_name}")
    except NotFound:
        return None
    return cast("dict[str, Any]", result)


def _create_service(
    client: Any,
    *,
    catalog: str,
    schema: str,
    api_name: str,
    connection_name: str,
    include_tool_selectors: list[str],
    comment: str,
) -> None:
    client.api_client.do(
        "POST",
        _SERVICES_API,
        query={"parent": f"schemas/{catalog}.{schema}", "mcp_service_id": api_name},
        body={
            "comment": comment,
            "config": {
                "connection": {"name": f"connections/{connection_name}"},
                "include_tool_selectors": include_tool_selectors,
            },
        },
    )


def _patch_config(client: Any, full_name: str, *, field: str, value: Any) -> None:
    """PATCH one ``config.<field>`` on the service via ``update_mask``."""
    client.api_client.do(
        "PATCH",
        f"{_SERVICES_API}/{full_name}",
        query={"update_mask": f"config.{field}"},
        body={"config": {field: value}},
    )


def _delete_service(client: Any, full_name: str) -> None:
    from databricks.sdk.errors import NotFound

    try:
        client.api_client.do("DELETE", f"{_SERVICES_API}/{full_name}")
    except NotFound:
        pass


def _grant_execute(client: Any, full_name: str, principals: list[str]) -> None:
    if not principals:
        return
    client.api_client.do(
        "PATCH",
        f"{_PERMISSIONS_API}/{full_name}",
        body={"changes": [{"principal": p, "add": ["EXECUTE"]} for p in principals]},
    )


# ---------------------------------------------------------------------------
# wire-shape builders (friendly config -> platform Beta body)
# ---------------------------------------------------------------------------


def build_rate_limits(svc: McpServiceSpec) -> list[dict[str, Any]]:
    """Map ``rate_limits`` config to the MCP-services wire body.

    Note the platform quirk confirmed in the spike: the request-count field is
    ``tokens`` (not the serving-endpoint ``calls``), and only per-principal
    keys on a ``minute`` window are accepted.
    """
    return [
        {
            "key": _RATE_LIMIT_KEY[rl.key],
            "principal": rl.principal,
            "renewal_period": _RENEWAL_PERIOD[rl.renewal_period],
            "tokens": rl.calls,
        }
        for rl in svc.rate_limits
    ]


def build_service_policies(svc: McpServiceSpec) -> list[dict[str, Any]]:
    """Map ``service_policies`` config to the MCP-services wire body."""
    policies: list[dict[str, Any]] = []
    for policy in svc.service_policies:
        handler = f"functions/{policy.handler}" if policy.policy_type == "custom" else policy.handler
        entry: dict[str, Any] = {
            "name": policy.name,
            "policy_type": _POLICY_TYPE[policy.policy_type],
            "handler": handler,
        }
        if policy.options:
            entry["options"] = policy.options
        policies.append(entry)
    return policies


# ---------------------------------------------------------------------------
# provisioning / teardown (called by the adapter, one level up)
# ---------------------------------------------------------------------------


def _reconcile_governance(client: Any, full_name: str, svc: McpServiceSpec) -> None:
    """PATCH the governance surface (selectors, policies, rate limits, grants)."""
    _patch_config(client, full_name, field="include_tool_selectors", value=list(svc.include_tool_selectors))
    _patch_config(client, full_name, field="service_policies", value=build_service_policies(svc))
    _patch_config(client, full_name, field="rate_limits", value=build_rate_limits(svc))
    _grant_execute(client, full_name, list(svc.grants.execute))


def provision_mcp_services(
    client: Any | None,
    spec: RestMcpPayloadSpec,
    connection_names: dict[str, str],
    *,
    instance_name: str,
    target: str,
    catalog: str,
    schema_prefix: str,
    deployer: str = "",
    dry_run: bool = False,
) -> list[str]:
    """Create + reconcile the MCP Service for each API whose block is enabled.

    Idempotent: the securable is create-if-absent, then its governance surface
    is PATCHed to the desired state on every deploy.  ``connection_names`` maps
    ``api_name -> connection name`` (from the connection layer's state) and the
    service is built *from* that connection.  Degrades to a clear warning (never
    a crash) when the Beta preview is off, so a connection-only deploy still
    succeeds.  Returns the provisioned service full names.
    """
    provisioned: list[str] = []
    for api in spec.apis:
        svc = api.mcp_service
        if svc is None or not svc.enabled:
            continue
        svc_catalog, svc_schema = _resolve_target(svc, catalog=catalog, schema_prefix=schema_prefix)
        full_name = mcp_service_full_name(svc_catalog, svc_schema, api.name)

        if client is None or dry_run:
            print(f"  [rest_mcp] (dry-run) would provision MCP Service {full_name!r} (govern-only, Beta)")
            provisioned.append(full_name)
            continue

        connection = connection_names.get(api.name)
        if not connection:
            print(f"  WARNING: [rest_mcp] no connection for api {api.name!r}; skipping MCP Service {full_name!r}")
            continue

        try:
            _ensure_schema(client, svc_catalog, svc_schema)
            if _get_service(client, full_name) is None:
                _create_service(
                    client,
                    catalog=svc_catalog,
                    schema=svc_schema,
                    api_name=api.name,
                    connection_name=connection,
                    include_tool_selectors=list(svc.include_tool_selectors),
                    comment=f"rest_mcp governed MCP Service: {instance_name}/{api.name}",
                )
                print(f"  [rest_mcp] created MCP Service {full_name!r} from connection {connection!r}")
            else:
                print(f"  [rest_mcp] MCP Service {full_name!r} exists; reconciling governance")
            _reconcile_governance(client, full_name, svc)
        except Exception as exc:  # noqa: BLE001 -- degrade gracefully only for a disabled Beta preview
            if _is_preview_off(exc):
                print(
                    f"  WARNING: [rest_mcp] MCP Service preview unavailable; skipping {full_name!r} "
                    f"(connection remains usable via type: external). Detail: {exc}"
                )
                continue
            raise

        provisioned.append(full_name)
        save_state(
            client,
            instance_name,
            MCP_SERVICE_BRICK_TYPE,
            api.name,
            target,
            {"service": full_name, "connection": connection, "catalog": svc_catalog, "schema": svc_schema},
            action="create",
            deployer=deployer,
            resource_id=full_name,
        )
    return provisioned


def loaded_mcp_services(
    client: Any | None,
    spec: RestMcpPayloadSpec,
    *,
    instance_name: str,
    target: str,
) -> dict[str, str]:
    """Map ``api_name -> MCP Service full name`` from persisted state."""
    out: dict[str, str] = {}
    for api in spec.apis:
        state = load_state(client, instance_name, MCP_SERVICE_BRICK_TYPE, api.name, target)
        service = state.get("service")
        if service:
            out[api.name] = str(service)
    return out


def destroy_mcp_services(
    client: Any | None,
    spec: RestMcpPayloadSpec,
    *,
    instance_name: str,
    target: str,
    dry_run: bool = False,
) -> list[str]:
    """Delete each provisioned MCP Service and clear its state (best-effort).

    Only services recorded in state are torn down, so an instance that never
    enabled path C is a no-op.  Runs *before* connection teardown (the service
    references the connection).
    """
    removed: list[str] = []
    for api in spec.apis:
        state = load_state(client, instance_name, MCP_SERVICE_BRICK_TYPE, api.name, target)
        full_name = state.get("service")
        if not full_name:
            continue
        full_name = str(full_name)
        removed.append(full_name)
        if client is None or dry_run:
            print(f"  [rest_mcp] (dry-run) would delete MCP Service {full_name!r}")
            continue
        _delete_service(client, full_name)
        delete_state(client, instance_name, MCP_SERVICE_BRICK_TYPE, api.name, target)
        print(f"  [rest_mcp] deleted MCP Service {full_name!r}")
    return removed


__all__ = [
    "MCP_SERVICE_BRICK_TYPE",
    "build_rate_limits",
    "build_service_policies",
    "default_schema",
    "destroy_mcp_services",
    "loaded_mcp_services",
    "mcp_service_full_name",
    "provision_mcp_services",
]
