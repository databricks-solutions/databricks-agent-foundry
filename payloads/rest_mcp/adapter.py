"""The built-in ``rest_mcp`` App payload adapter (issue #326).

Selected by ``payload.adapter = rest_mcp``.  It plugs the config-driven
REST->MCP proxy into the framework's
:class:`~agent_foundry.deploy.payload_adapters.AppPayloadAdapter` seam: it
resolves the payload's typed spec, produces the lean neutral
:class:`AppPayloadSpec` / :class:`AppInfraSpec` the generic App host consumes,
and (in a follow-on slice) provisions the payload-owned Unity Catalog surface.

Deploy-time resolution lives in :mod:`payloads.rest_mcp.deploy` and is imported
lazily so the framework adapter registry stays free of SDK/runtime imports at
load time (mirroring :class:`payloads.agent.adapter.AgentPayloadAdapter`).

The one net-new capability -- the governed hop-1 Unity Catalog ``is_mcp``
connection per API -- is provisioned in :meth:`after_reconcile` rather than
:meth:`provision`, because the connection targets the hosting App's URL, which
is only known *after* the App resource is reconciled.  :meth:`provision` runs
earlier (before the App exists on a first deploy), so it merely stashes the
instance directory + target into the shared variable bag for
:meth:`after_reconcile` to pick up (the bag flows by reference through the
App-host deploy).  Connection CRUD + state live in
:mod:`payloads.rest_mcp.deploy.connection`; payload-owned scaffold declarations
provide the proxy manifest and its paired agent MCP attachment.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from agent_foundry._instance_layout import has_payload
from agent_foundry.deploy.app_host import AppInfraSpec, AppPayloadSpec
from agent_foundry.scaffold_spec import ScaffoldDecl, ScaffoldSource

if TYPE_CHECKING:
    from databricks.sdk import WorkspaceClient

    from agent_foundry.bricks import (
        BrickResult,
        DeployContext,
        DestroyContext,
        DestroyPlanItem,
    )
    from agent_foundry.smoke import SmokeProbe, SmokeTarget

# Shared-variable-bag keys used to hand the instance dir + target from
# ``provision`` (full ``DeployContext``) to ``after_reconcile`` (which only
# receives ``ws`` / ``app_name`` / ``variables``).
_PAYLOAD_PKG_ROOT = Path(__file__).resolve().parent
_STASH_INSTANCE_DIR = "rest_mcp__instance_dir"
_STASH_TARGET = "rest_mcp__target"
_DISCOVERY_PREFIX = "mcp-"
_DEFAULT_APP_SUFFIX = "-app"


class RestMcpPayloadAdapter:
    """``payload.adapter = rest_mcp``: the config-driven REST->MCP proxy host."""

    name = "rest_mcp"

    def applies_to(self, instance_dir: Path, config: Mapping[str, object]) -> bool:  # noqa: ARG002
        return has_payload(instance_dir, self.name)

    def app_resource_name(self, default: str) -> str:
        """``dev-github-app`` -> ``mcp-dev-github``.

        Unity Gateway and AI Playground only discover App-hosted MCP servers
        whose App name starts with ``mcp-``.
        """
        return f"{_DISCOVERY_PREFIX}{default.removesuffix(_DEFAULT_APP_SUFFIX)}"

    def build_payload_spec(
        self,
        instance_dir: Path | None,
        variables: Mapping[str, str],
        spec: Mapping[str, object],
    ) -> AppPayloadSpec:
        from payloads.rest_mcp.deploy.spec import build_rest_mcp_payload_spec
        from payloads.rest_mcp.runtime.config import resolve_rest_mcp_spec

        # Validate the payload spec up front so a malformed proxy config fails
        # at deploy time, not on the first tool call in the container.
        resolve_rest_mcp_spec(instance_dir, spec)
        return build_rest_mcp_payload_spec(instance_dir, variables)

    def build_infra(
        self,
        instance_dir: Path | None,
        variables: Mapping[str, str],  # noqa: ARG002 (infra is spec-independent for now)
        *,
        target: str,  # noqa: ARG002
    ) -> AppInfraSpec:
        from payloads.rest_mcp.deploy.spec import build_rest_mcp_infra

        return build_rest_mcp_infra(instance_dir)

    def after_reconcile(
        self,
        ws: WorkspaceClient | None,
        app_name: str,
        variables: Mapping[str, str],
        *,
        dry_run: bool,
    ) -> None:
        """Provision the governed ``is_mcp`` UC connection for each API, then
        (only for APIs that opt in) the optional MCP Service governance layer.

        Runs after the App resource is reconciled, so the App URL + its service
        principal (``service_principal_client_id`` / ``service_principal_id``)
        are available -- required to point the connection at
        ``{app_url}/mcp/<api>/`` and authenticate to the App as its own SP via
        OAuth M2M.  Gates on the variables :meth:`provision` stashed; a no-op
        when they are absent (defensive) or the App identity cannot be resolved.

        The MCP Service (path C) is strictly layered *after* the connection: it
        is built from the just-provisioned connection and never mutates it.
        """
        from pathlib import Path

        from payloads.rest_mcp.deploy.connection import (
            grant_app_sp_can_use,
            loaded_connections,
            provision_connections,
        )
        from payloads.rest_mcp.deploy.mcp_service import provision_mcp_services
        from payloads.rest_mcp.runtime.config import load_rest_mcp_spec

        instance_dir_raw = variables.get(_STASH_INSTANCE_DIR)
        target = variables.get(_STASH_TARGET, "")
        if not instance_dir_raw or not target:
            return
        instance_dir = Path(instance_dir_raw)
        instance_name = instance_dir.name
        spec = load_rest_mcp_spec(instance_dir)
        if len(spec.apis) > 1:
            print(
                f"  NOTE: [rest_mcp] {app_name!r} serves {len(spec.apis)} APIs; Unity Gateway discovery "
                "only finds a single /mcp server per App, so use the UC connections instead."
            )

        if dry_run or ws is None:
            grant_app_sp_can_use(None, app_name, "", dry_run=True)
            provision_connections(
                None,
                spec,
                instance_name=instance_name,
                target=target,
                app_url="",
                app_sp_client_id="",
                app_sp_id="",
                workspace_host="",
                dry_run=True,
            )
            provision_mcp_services(
                None,
                spec,
                {},
                instance_name=instance_name,
                target=target,
                catalog=variables.get("catalog", ""),
                schema_prefix=variables.get("schema_prefix", ""),
                dry_run=True,
            )
            return

        app_url, app_sp_client_id, app_sp_id, workspace_host = self._resolve_app_identity(ws, app_name)
        if not app_url or not app_sp_client_id or not app_sp_id:
            print(
                f"  WARNING: [rest_mcp] App URL / service principal unavailable for {app_name!r}; "
                "skipping UC connection provisioning."
            )
            return

        grant_app_sp_can_use(ws, app_name, app_sp_client_id)
        provision_connections(
            ws,
            spec,
            instance_name=instance_name,
            target=target,
            app_url=app_url,
            app_sp_client_id=app_sp_client_id,
            app_sp_id=app_sp_id,
            workspace_host=workspace_host,
            deployer=variables.get("deployer", ""),
        )

        # Path C (optional, opt-in per API) -- built from the connections above.
        provision_mcp_services(
            ws,
            spec,
            loaded_connections(ws, spec, instance_name=instance_name, target=target),
            instance_name=instance_name,
            target=target,
            catalog=variables.get("catalog", ""),
            schema_prefix=variables.get("schema_prefix", ""),
            deployer=variables.get("deployer", ""),
        )

    @staticmethod
    def _resolve_app_identity(ws: WorkspaceClient, app_name: str) -> tuple[str, str, str, str]:
        """Return ``(app_url, sp_client_id, sp_id, workspace_host)`` for the App.

        ``sp_client_id`` is the App service principal's OAuth ``client_id``
        (``applicationId``) stored in the connection; ``sp_id`` is its numeric
        id, used to mint the M2M secret.
        """
        workspace_host = (getattr(ws.config, "host", "") or "").rstrip("/")
        try:
            app = ws.apps.get(app_name)
        except Exception as exc:  # noqa: BLE001 (best-effort; a missing App must not crash deploy)
            print(f"  WARNING: [rest_mcp] could not read App {app_name!r}: {exc}")
            return "", "", "", workspace_host
        app_url = (getattr(app, "url", "") or "").rstrip("/")
        sp_client_id = getattr(app, "service_principal_client_id", "") or ""
        sp_id = str(getattr(app, "service_principal_id", "") or "")
        return app_url, sp_client_id, sp_id, workspace_host

    def provision(self, ctx: DeployContext) -> int:
        """Stash the instance dir + target for :meth:`after_reconcile`.

        Connection provisioning itself is deferred to ``after_reconcile`` (the
        App URL is only known post-reconcile); here we only hand the context it
        needs across the App-host boundary via the shared variable bag.
        """
        ctx.variables[_STASH_INSTANCE_DIR] = str(ctx.instance_dir)
        ctx.variables[_STASH_TARGET] = ctx.target
        return 0

    def plan_destroy(self, ctx: DestroyContext) -> list[DestroyPlanItem]:
        """Plan removal of every provisioned MCP Service, then UC connection.

        Services are listed first because they reference the connection and must
        be torn down before it (reverse of create).
        """
        from agent_foundry.bricks import DestroyPlanItem
        from payloads.rest_mcp.deploy.connection import connection_name, loaded_connections
        from payloads.rest_mcp.deploy.mcp_service import loaded_mcp_services
        from payloads.rest_mcp.runtime.config import load_rest_mcp_spec

        spec = load_rest_mcp_spec(ctx.instance_dir)
        instance_name = ctx.instance_dir.name
        services = loaded_mcp_services(ctx.ws, spec, instance_name=instance_name, target=ctx.target)
        known = loaded_connections(ctx.ws, spec, instance_name=instance_name, target=ctx.target)
        plan = [
            DestroyPlanItem(
                brick=self.name,
                sub_instance=api_name,
                resource=f"UC MCP Service {full_name}",
                destructive=True,
            )
            for api_name, full_name in services.items()
        ]
        plan += [
            DestroyPlanItem(
                brick=self.name,
                sub_instance=api.name,
                resource=f"UC MCP connection {known.get(api.name) or connection_name(instance_name, api.name)}",
                destructive=True,
            )
            for api in spec.apis
        ]
        return plan

    def destroy(self, ctx: DestroyContext) -> list[BrickResult]:
        """Delete each API's MCP Service (if any), then its UC connection.

        Reverse of create: services reference the connection, so they are torn
        down first, then the connection (which also revokes its minted secret).
        """
        from agent_foundry.bricks import BrickResult, deleted_refs
        from payloads.rest_mcp.deploy.connection import destroy_connections
        from payloads.rest_mcp.deploy.mcp_service import destroy_mcp_services
        from payloads.rest_mcp.runtime.config import load_rest_mcp_spec

        spec = load_rest_mcp_spec(ctx.instance_dir)
        instance_name = ctx.instance_dir.name
        services = destroy_mcp_services(
            ctx.ws,
            spec,
            instance_name=instance_name,
            target=ctx.target,
            dry_run=ctx.dry_run,
        )
        connections = destroy_connections(
            ctx.ws,
            spec,
            instance_name=instance_name,
            target=ctx.target,
            dry_run=ctx.dry_run,
        )
        results = [
            BrickResult(
                name=f"{self.name}:service:{name}",
                status="ok",
                deleted=deleted_refs(ctx, 0, "mcp_service", name),
            )
            for name in services
        ]
        results += [
            BrickResult(
                name=f"{self.name}:{name}",
                status="ok",
                deleted=deleted_refs(ctx, 0, "uc_connection", name),
            )
            for name in connections
        ]
        return results

    def load_state_outputs(self, ctx: DeployContext) -> dict[str, str]:
        """Republish connection + MCP Service names into the variable bag.

        ``rest_mcp_connection__<api>`` (consumed by each ``server.yml``) and,
        when path C is enabled, ``rest_mcp_service__<api>`` (the 3-level service
        name, for reference / a future gateway-consumption phase).
        """
        from payloads.rest_mcp.deploy.connection import loaded_connections
        from payloads.rest_mcp.deploy.mcp_service import loaded_mcp_services
        from payloads.rest_mcp.runtime.config import load_rest_mcp_spec

        spec = load_rest_mcp_spec(ctx.instance_dir)
        instance_name = ctx.instance_dir.name
        known = loaded_connections(ctx.ws, spec, instance_name=instance_name, target=ctx.target)
        services = loaded_mcp_services(ctx.ws, spec, instance_name=instance_name, target=ctx.target)
        outputs = {f"rest_mcp_connection__{api_name}": conn for api_name, conn in known.items()}
        outputs.update({f"rest_mcp_service__{api_name}": full for api_name, full in services.items()})
        return outputs

    def scaffolds(self) -> Sequence[ScaffoldDecl]:
        """Declare the proxy manifest and its paired agent attachment."""
        scaffold_root = _PAYLOAD_PKG_ROOT / "_scaffold"
        return (
            ScaffoldDecl(
                name="rest_mcp",
                group="rest_mcp payload",
                display="REST MCP Payload",
                summary="REST-backed MCP proxy manifest plus an agent attachment.",
                sources=(
                    ScaffoldSource(
                        source=scaffold_root / "payload.yml",
                        destination="payloads/rest_mcp/payload.yml",
                        shape="file",
                    ),
                    ScaffoldSource(
                        source=scaffold_root / "server.yml",
                        destination="payloads/agent/mcp_servers/example_api/server.yml",
                        shape="file",
                    ),
                ),
            ),
        )

    def smoke_probes(self, target: SmokeTarget) -> list[SmokeProbe]:
        """Each API's declared tools listed through its UC MCP gateway (``agent-foundry smoke``)."""
        from payloads.rest_mcp.smoke import rest_mcp_smoke_probes

        return rest_mcp_smoke_probes(target)


__all__ = ["RestMcpPayloadAdapter"]
