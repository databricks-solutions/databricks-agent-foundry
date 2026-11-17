"""Deploy-time provisioning of the governed hop-1 UC connection(s) (issue #326).

Live-validated in a workspace: each API in the payload spec is fronted by its
own ``is_mcp`` Unity Catalog **HTTP connection** pointing at the hosting App's
per-API MCP mount ``{app_url}/mcp/<api>/`` (see
:mod:`payloads.rest_mcp.runtime.server.app`).  Agents consume it through the
framework's existing ``type: external`` MCP surface
(``/api/2.0/mcp/external/<connection_name>``), so the proxy needs no
agent-runtime change and the hop stays governed by Unity Catalog.

Authentication is **OAuth M2M as the hosting App's own service principal**: the
connection stores that SP's ``client_id`` (``app.service_principal_client_id``)
plus a freshly-minted OAuth secret and the workspace token endpoint.  The
platform does not put an App's SP on its own permission list, so deploy grants
it ``CAN_USE`` (:func:`grant_app_sp_can_use`); without that the App front door
rejects hop-1 with an empty 401 (issue #517).  This is shared, governed access:
Unity Catalog checks ``USE CONNECTION`` on the caller, but the App only ever
sees its own SP.  U2M mapping was rejected because it drives a browser
authorization-code flow whose redirect URI is not registered on the App's OAuth
client (and the workspace does not support DCR).

Secret lifecycle: one OAuth secret is minted per connection at create (and
again if the App URL or service principal rotated).  Recreating the App
under a stable name mints a new SP; leaving the UC connection as-is then
401s hop-1.  The secret id is persisted in workspace state so
:func:`destroy_connections` can revoke it -- a 1:1 connection<->secret
lifecycle.

Why raw ``api_client.do`` for the connection CRUD: the typed SDK does not model
the ``is_mcp_connection`` options, so the exact option set is sent verbatim.
Secret mint/revoke uses the typed ``service_principal_secrets_proxy`` API.  All
logic here is SDK-thin and pure so it stays unit-testable with a fake client --
the adapter's ``after_reconcile`` hook supplies the real
:class:`WorkspaceClient` and the post-reconcile App identity.

NOTE: the optional UC **MCP Service** governance layer (tool selection, service
policies, rate limits, grants) is deliberately kept out of this module -- it is
purely additive on top of the connection and lands, if enabled, in a separate
``mcp_service`` module + config block so the two layers never entangle.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, cast

from agent_foundry.state import delete_state, load_state, save_state

if TYPE_CHECKING:
    from payloads.rest_mcp.contracts import RestMcpPayloadSpec

# ``brick_type`` partition for this payload's workspace-backed state
# (one ``<sub_instance>`` == one API name).
CONNECTION_BRICK_TYPE = "rest_mcp"

_CONNECTIONS_API = "/api/2.1/unity-catalog/connections"
# Per-API MCP mount served by the host; MUST match ``build_app`` in
# ``runtime/server/app.py`` (``/mcp/<api>``).  The trailing slash is required:
# the App's Starlette mount 307-redirects ``/mcp/<api>`` -> ``/mcp/<api>/`` and
# the AI Gateway / external-MCP proxy does not follow the redirect, so the tool
# call would otherwise arrive with no JSON-RPC response.
_MCP_MOUNT = "/mcp"
_OAUTH_SCOPE = "all-apis"


def connection_name(instance_name: str, api_name: str) -> str:
    """Deterministic UC connection name for one API mount.

    Sanitised to the safe securable charset so an instance directory with
    hyphens/dots still yields a valid Unity Catalog name.
    """
    slug = re.sub(r"[^a-z0-9_-]", "_", instance_name.lower())
    return f"{slug}_{api_name}_mcp"


def _token_endpoint(workspace_host: str) -> str:
    return f"{workspace_host.rstrip('/')}/oidc/v1/token"


def _mcp_target_url(app_url: str, api_name: str) -> str:
    """The App URL a connection resolves to: ``{app_url}/mcp/<api>/``."""
    return f"{app_url.rstrip('/')}{_MCP_MOUNT}/{api_name}/"


def build_connection_options(
    *,
    app_url: str,
    api_name: str,
    app_sp_client_id: str,
    client_secret: str,
    workspace_host: str,
) -> dict[str, str]:
    """Build the ``options`` map for an ``is_mcp`` OAuth-M2M connection.

    ``host`` is the App root and ``base_path`` the per-API mount (trailing
    slash), so the connection resolves to ``{app_url}/mcp/<api>/``.  The
    credentials are the hosting App's service-principal ``client_id`` + a minted
    OAuth secret, exchanged at the workspace OIDC token endpoint (M2M / client
    credentials -- no browser redirect).
    """
    return {
        "host": app_url.rstrip("/"),
        "base_path": f"{_MCP_MOUNT}/{api_name}/",
        "is_mcp_connection": "true",
        "client_id": app_sp_client_id,
        "client_secret": client_secret,
        "token_endpoint": _token_endpoint(workspace_host),
        "oauth_scope": _OAUTH_SCOPE,
    }


def _get_connection(client: Any, name: str) -> dict[str, Any] | None:
    """Return the connection dict, or ``None`` when it does not exist yet."""
    from databricks.sdk.errors import NotFound

    try:
        result = client.api_client.do("GET", f"{_CONNECTIONS_API}/{name}")
    except NotFound:
        return None
    return cast("dict[str, Any]", result)


def _create_connection(client: Any, name: str, options: dict[str, str], *, comment: str) -> None:
    client.api_client.do(
        "POST",
        _CONNECTIONS_API,
        body={
            "name": name,
            "connection_type": "HTTP",
            "options": options,
            "comment": comment,
        },
    )


def _update_connection(client: Any, name: str, options: dict[str, str]) -> None:
    client.api_client.do("PATCH", f"{_CONNECTIONS_API}/{name}", body={"options": options})


def _connection_stale(
    existing: dict[str, Any],
    *,
    app_url: str,
    api_name: str,
    app_sp_client_id: str,
    app_sp_id: str,
    state: dict[str, Any],
) -> bool:
    """True when hop-1 would still point at a previous App URL or SP."""
    opts = existing.get("options") or {}
    host = str(opts.get("host") or "").rstrip("/")
    path = str(opts.get("base_path") or "")
    client_id = str(opts.get("client_id") or "")
    stored_sp = str(state.get("sp_id") or "")
    expected_host = app_url.rstrip("/")
    expected_path = f"{_MCP_MOUNT}/{api_name}/"
    if host and host != expected_host:
        return True
    if path and path != expected_path:
        return True
    if client_id and client_id != app_sp_client_id:
        return True
    if stored_sp and stored_sp != app_sp_id:
        return True
    if not host or not path:
        return True
    return not client_id and not stored_sp


def _save_connection_state(
    client: Any,
    *,
    instance_name: str,
    api_name: str,
    target: str,
    name: str,
    target_url: str,
    app_sp_id: str,
    secret_id: str,
    deployer: str,
    action: str,
) -> None:
    save_state(
        client,
        instance_name,
        CONNECTION_BRICK_TYPE,
        api_name,
        target,
        {"connection": name, "url": target_url, "sp_id": app_sp_id, "sp_secret_id": secret_id},
        action=action,
        deployer=deployer,
        resource_id=name,
    )


def _delete_connection(client: Any, name: str) -> None:
    from databricks.sdk.errors import NotFound

    try:
        client.api_client.do("DELETE", f"{_CONNECTIONS_API}/{name}")
    except NotFound:
        pass


def _mint_sp_secret(client: Any, sp_id: str) -> tuple[str, str]:
    """Mint an OAuth (M2M) secret for the App SP; return ``(secret, secret_id)``."""
    resp = client.service_principal_secrets_proxy.create(service_principal_id=sp_id)
    return str(getattr(resp, "secret", "") or ""), str(getattr(resp, "id", "") or "")


def _revoke_sp_secret(client: Any, sp_id: str, secret_id: str) -> None:
    """Best-effort revoke of a minted App SP secret (idempotent)."""
    from databricks.sdk.errors import NotFound

    try:
        client.service_principal_secrets_proxy.delete(service_principal_id=sp_id, secret_id=secret_id)
    except NotFound:
        pass


def grant_app_sp_can_use(client: Any | None, app_name: str, app_sp_client_id: str, *, dry_run: bool = False) -> None:
    """Grant the App's service principal ``CAN_USE`` on its own App.

    ``update_permissions`` adds the entry without replacing the list, so a
    redeploy is idempotent.
    """
    if client is None or dry_run:
        print(f"  [rest_mcp] (dry-run) would grant the App service principal CAN_USE on {app_name!r}")
        return
    from databricks.sdk.service.apps import AppAccessControlRequest, AppPermissionLevel

    client.apps.update_permissions(
        app_name,
        access_control_list=[
            AppAccessControlRequest(
                service_principal_name=app_sp_client_id,
                permission_level=AppPermissionLevel.CAN_USE,
            )
        ],
    )
    print(f"  [rest_mcp] granted the App service principal CAN_USE on {app_name!r}")


def provision_connections(
    client: Any | None,
    spec: RestMcpPayloadSpec,
    *,
    instance_name: str,
    target: str,
    app_url: str,
    app_sp_client_id: str,
    app_sp_id: str,
    workspace_host: str,
    deployer: str = "",
    dry_run: bool = False,
) -> list[str]:
    """Create or refresh the ``is_mcp`` UC connection for each API.

    Create-if-absent when the name is new.  Patch + mint a new App SP secret
    when the existing connection still targets a previous App URL or SP
    (stable canary names recreate the App).  Same URL+SP is left as-is.
    Dry-run / offline (``client is None`` or ``dry_run``) writes nothing.
    """
    names: list[str] = []
    for api in spec.apis:
        name = connection_name(instance_name, api.name)
        names.append(name)
        target_url = _mcp_target_url(app_url, api.name)

        if client is None or dry_run:
            print(f"  [rest_mcp] (dry-run) would provision UC MCP connection {name!r} -> {target_url}")
            continue

        existing = _get_connection(client, name)
        state = load_state(client, instance_name, CONNECTION_BRICK_TYPE, api.name, target)
        if existing is not None and not _connection_stale(
            existing,
            app_url=app_url,
            api_name=api.name,
            app_sp_client_id=app_sp_client_id,
            app_sp_id=app_sp_id,
            state=state,
        ):
            print(f"  [rest_mcp] UC MCP connection {name!r} already exists -> {target_url} (left as-is)")
            continue

        secret, secret_id = _mint_sp_secret(client, app_sp_id)
        options = build_connection_options(
            app_url=app_url,
            api_name=api.name,
            app_sp_client_id=app_sp_client_id,
            client_secret=secret,
            workspace_host=workspace_host,
        )
        if existing is None:
            _create_connection(
                client, name, options, comment=f"rest_mcp proxy (M2M app SP): {instance_name}/{api.name}"
            )
            print(f"  [rest_mcp] created UC MCP connection {name!r} -> {target_url}")
            action = "create"
        else:
            _update_connection(client, name, options)
            old_sp = str(state.get("sp_id") or "")
            old_secret = str(state.get("sp_secret_id") or "")
            if old_sp and old_secret:
                _revoke_sp_secret(client, old_sp, old_secret)
            print(f"  [rest_mcp] refreshed UC MCP connection {name!r} -> {target_url} (App SP or URL rotated)")
            action = "update"

        _save_connection_state(
            client,
            instance_name=instance_name,
            api_name=api.name,
            target=target,
            name=name,
            target_url=target_url,
            app_sp_id=app_sp_id,
            secret_id=secret_id,
            deployer=deployer,
            action=action,
        )
    return names


def loaded_connections(
    client: Any | None,
    spec: RestMcpPayloadSpec,
    *,
    instance_name: str,
    target: str,
) -> dict[str, str]:
    """Map ``api_name -> connection name`` from persisted state (empty if none)."""
    out: dict[str, str] = {}
    for api in spec.apis:
        state = load_state(client, instance_name, CONNECTION_BRICK_TYPE, api.name, target)
        connection = state.get("connection")
        if connection:
            out[api.name] = str(connection)
    return out


def destroy_connections(
    client: Any | None,
    spec: RestMcpPayloadSpec,
    *,
    instance_name: str,
    target: str,
    dry_run: bool = False,
) -> list[str]:
    """Delete each API's UC connection, revoke its minted secret, clear its state."""
    removed: list[str] = []
    for api in spec.apis:
        state = load_state(client, instance_name, CONNECTION_BRICK_TYPE, api.name, target)
        name = str(state.get("connection") or connection_name(instance_name, api.name))
        removed.append(name)
        if client is None or dry_run:
            print(f"  [rest_mcp] (dry-run) would delete UC MCP connection {name!r}")
            continue
        _delete_connection(client, name)
        sp_id = state.get("sp_id")
        secret_id = state.get("sp_secret_id")
        if sp_id and secret_id:
            _revoke_sp_secret(client, str(sp_id), str(secret_id))
        delete_state(client, instance_name, CONNECTION_BRICK_TYPE, api.name, target)
        print(f"  [rest_mcp] deleted UC MCP connection {name!r}")
    return removed


__all__ = [
    "CONNECTION_BRICK_TYPE",
    "build_connection_options",
    "connection_name",
    "destroy_connections",
    "grant_app_sp_can_use",
    "loaded_connections",
    "provision_connections",
]
