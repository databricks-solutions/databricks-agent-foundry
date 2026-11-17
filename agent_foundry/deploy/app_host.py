"""
Generic Databricks App host engine (issue #337)
================================================
Brick-agnostic building blocks for deploying and tearing down a
Databricks App.  This module owns the *infrastructure* half of the App
lifecycle -- the parts that are identical regardless of what runtime
code (the "payload") runs inside the container:

* wheel-based staging + ``databricks sync`` (:func:`sync_source`)
* App *spec* reconcile -- compute size, resource bindings, OBO scopes
  (:func:`reconcile_app_spec`)
* service-principal UC grants (:func:`grant_sp_permissions`)
* code deploy from the synced workspace path (:func:`deploy_app_code`)
* App tagging (:func:`tag_app`)
* teardown -- App delete + workspace dir cleanup (:func:`destroy_app`)

Layering contract (issue #337)
------------------------------
This module lives in the framework layer (``agent_foundry/deploy``) and
MUST NOT import anything from ``bricks/*`` or any payload-specific
config (e.g. ``AgentAppConfig``).  Payload bricks (``agent_app`` today,
``rest_mcp`` next) translate their own config into the neutral inputs
these helpers accept and orchestrate the calls -- interleaving any
payload-specific hooks (Lakebase, UC connection) inline.

The leading-underscore aliases at the bottom preserve the historical
names that ``bricks/app/scripts/deploy_agent_app.py`` re-exports
for backwards compatibility (tests + the ``agent_app deploy`` CLI verb).
"""

from __future__ import annotations

import inspect
import io
import subprocess
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.apps import (
    App,
    AppDeployment,
    AppDeploymentMode,
    AppResource,
    ComputeSize,
    TelemetryExportDestination,
    UnityCatalog,
)
from pydantic import BaseModel, ConfigDict

from agent_foundry._instance_layout import payload_root
from agent_foundry.config import TAG_KEY_MANAGED_BY
from agent_foundry.deploy.staging import build_deploy_staging, clean_workspace_dir
from agent_foundry.sql_statements import quote_dotted_name

if TYPE_CHECKING:
    from agent_foundry.deploy.payload_adapters import AppPayloadAdapter

# ---------------------------------------------------------------------------
# App payload contract (issue #337)
# ---------------------------------------------------------------------------


class AppPayloadSpec(BaseModel):
    """The runtime-code contract for a Databricks App payload.

    Describes ONLY what runs *inside* the App container -- never the
    surrounding infrastructure (compute size, resource bindings,
    privileges, OBO scopes), which the App host owns.  A payload brick
    (``agent_app`` today, ``rest_mcp`` next) resolves its own config into
    this neutral spec and hands it to the engine; swapping payloads means
    swapping the spec, not the App-host machinery.

    Attributes
    ----------
    command
        The container entrypoint argv written to ``app.yaml`` ``command``.
    env
        ``app.yaml`` ``env`` entries, verbatim.  Each entry is a dict that
        may carry either a literal ``value`` or a ``valueFrom`` reference
        (resolved from an App resource at runtime) -- both are preserved.
    requirements_extras
        Wheel extras the container must install (e.g. ``("runtime",)`` or
        ``("runtime", "extraction")``) so the payload's imports resolve.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    command: list[str] = []
    env: list[dict[str, Any]] = []
    requirements_extras: tuple[str, ...] = ("runtime",)


class AppInfraSpec(BaseModel):
    """The infrastructure inputs the App host needs to reconcile an App.

    The neutral counterpart to :class:`AppPayloadSpec`: where the payload
    spec describes *what runs inside* the container, this bundles *what
    the App host provisions around it* -- compute size, resource
    bindings, OBO scopes and service-principal grants.  A payload
    translates its own config into this spec (e.g. the agent payload maps
    ``app/config.yml`` here); the host stays free of payload config types.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    description: str = ""
    app_resources: list[dict[str, Any]] = []
    user_scopes: list[str] = []
    sp_grants: list[dict[str, Any]] = []
    compute_size: str = "medium"
    instances: int | None = None
    telemetry_destination: str | None = None


def render_app_yaml(payload: AppPayloadSpec) -> dict[str, Any]:
    """Render the payload's runtime contract into an ``app.yaml`` mapping.

    Returns an empty mapping when the payload defines neither a command
    nor any env -- callers skip the upload in that case.
    """
    data: dict[str, Any] = {}
    if payload.command:
        data["command"] = list(payload.command)
    if payload.env:
        # Copy each entry so ``valueFrom`` / ``value`` keys survive intact.
        data["env"] = [dict(entry) for entry in payload.env]
    return data


def upload_app_yaml(
    ws: WorkspaceClient | None,
    workspace_path: str,
    payload: AppPayloadSpec,
    dry_run: bool,
) -> None:
    """Render *payload* to ``app.yaml`` and upload it to the workspace.

    The payload is expected to be fully resolved (``${var.*}`` already
    substituted by the owning brick); the engine only serialises and
    uploads it -- it never reads package data or applies substitution.
    """
    data = render_app_yaml(payload)
    if not data:
        return

    target = f"{workspace_path}/app.yaml"
    if dry_run:
        print(f"  [dry-run] Would upload resolved app.yaml to {target}")
        return

    # Past the dry-run gate the caller always supplies a real client.
    assert ws is not None, "non-dry-run path requires a WorkspaceClient"

    from databricks.sdk.service.workspace import ImportFormat

    print(f"  Uploading resolved app.yaml to {target} ...")
    rendered = yaml.dump(data, default_flow_style=False, sort_keys=False)
    ws.workspace.upload(
        path=target,
        content=io.BytesIO(rendered.encode("utf-8")),
        format=ImportFormat.AUTO,
        overwrite=True,
    )


def app_exists(ws: WorkspaceClient, name: str) -> bool:
    """True iff a Databricks App named *name* exists in the workspace.

    Catches the SDK's typed :class:`databricks.sdk.errors.NotFound`
    first (substring matching against ``str(exc)`` is unreliable
    across SDK versions) and falls back to the legacy substring check
    so older wrappers / test mocks still resolve to ``False``.
    """
    from databricks.sdk.errors import NotFound

    try:
        ws.apps.get(name)
        return True
    except NotFound:
        return False
    except Exception as exc:
        msg = str(exc)
        if "NOT_FOUND" in msg or "RESOURCE_DOES_NOT_EXIST" in msg:
            return False
        print(f"  WARNING: apps.get('{name}') failed: {exc}")
        return False


def format_sdk_error(exc: BaseException) -> str:
    """Human-readable SDK/API error; ``DatabricksError(None)`` prints as ``None``."""
    code = getattr(exc, "error_code", None)
    text = str(exc)
    if not text or text == "None":
        text = repr(exc)
    parts = [str(code)] if code else []
    parts.append(text)
    extra = getattr(exc, "kwargs", None)
    if extra:
        parts.append(repr(extra))
    details = getattr(exc, "details", None)
    if details:
        parts.append(repr(details))
    return ": ".join(parts)


def is_app_name_not_ready(exc: BaseException) -> bool:
    """True when create failed because the name is still reserved after delete."""
    text = format_sdk_error(exc).lower()
    if str(exc) in ("", "None") or repr(exc).endswith("(None)"):
        return True
    return any(marker in text for marker in ("already exist", "deleting", "conflict", "in use"))


def wait_until_app_gone(
    ws: WorkspaceClient,
    name: str,
    *,
    timeout_seconds: float = 900,
    poll_seconds: float = 10,
    gone_polls: int = 3,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
) -> bool:
    """True when ``apps.get`` reports the App is gone, else False on timeout.

    ``apps.delete`` is asynchronous. ``apps.get`` can 404 while the platform
    still holds the name; a create then fails with an empty ``DatabricksError``.
    Require *gone_polls* consecutive misses before treating the name as free.
    """
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    deadline = clock() + timeout_seconds
    consecutive = 0
    while clock() < deadline:
        if app_exists(ws, name):
            consecutive = 0
        else:
            consecutive += 1
            if consecutive >= gone_polls:
                return True
        sleep(poll_seconds)
    return False


def create_app_with_retry(
    ws: WorkspaceClient,
    app: App,
    *,
    attempts: int = 6,
    retry_seconds: float = 15,
    sleep: Callable[[float], None] | None = None,
) -> None:
    """``apps.create_and_wait``, retrying while a just-deleted name is reserved."""
    sleep = sleep or time.sleep
    last: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            ws.apps.create_and_wait(app)
            return
        except Exception as exc:
            last = exc
            print(f"  App create failed (attempt {attempt}/{attempts}): {format_sdk_error(exc)}")
            if attempt >= attempts or not is_app_name_not_ready(exc):
                raise
            sleep(retry_seconds)
    if last is not None:
        raise last


def _compute_state(app: Any) -> str:
    status = getattr(app, "compute_status", None)
    state = getattr(status, "state", None) if status is not None else None
    if state is None:
        return ""
    return str(getattr(state, "value", state)).upper()


def _is_deleting(app: Any) -> bool:
    state = _compute_state(app)
    return state == "DELETING" or state.endswith("_DELETING")


def resolve_payload_readme(payload: AppPayloadAdapter, instance_dir: Path) -> Path | None:
    """Return the instance payload README, else the adapter package's README."""
    candidates = (
        payload_root(instance_dir, payload.name) / "README.md",
        Path(inspect.getfile(type(payload))).parent / "README.md",
    )
    return next((path for path in candidates if path.is_file()), None)


def sync_source(
    ws: WorkspaceClient,
    app_name: str,
    deployer: str,
    dry_run: bool,
    *,
    instance_dir: Path,
    solution_root: str | None = None,
    profile: str | None = None,
    extras: tuple[str, ...] = ("runtime",),
    readme: Path | None = None,
) -> str:
    """Build a wheel-based staging dir and sync it to the workspace.

    Issue #112: ships exactly the framework wheel + ``requirements.txt``
    + the instance dir; all runtime imports come from the wheel
    installed into the App venv.

    Parameters
    ----------
    instance_dir
        The application instance directory (the dir with
        ``foundry.toml``).  Copied verbatim into
        ``<staging>/instances/<name>/``.
    extras
        Wheel extras the payload needs installed in the container
        (e.g. ``("runtime", "extraction")`` for the agent's file
        extractor).  Lock-derived ``-c`` constraint files are always
        included.
    readme
        Optional payload README copied to the staging root as
        ``README.md`` (see :func:`resolve_payload_readme`).

    Returns the workspace path the staging dir was synced to.
    """
    if solution_root:
        workspace_path = f"{solution_root}/source"
    else:
        workspace_path = f"/Users/{deployer}/{app_name}"

    if dry_run:
        print(f"  [dry-run] Would sync to {workspace_path}")
        return workspace_path

    print("  Building deploy staging dir (wheel + requirements + instance) ...")
    staging_handle = build_deploy_staging(instance_dir, extras=extras, readme=readme)
    try:
        staging_root = Path(staging_handle.name)
        clean_workspace_dir(ws, workspace_path, dry_run=False)
        print(f"  Syncing staging dir to {workspace_path} ...")
        cmd = [
            "databricks",
            "sync",
            str(staging_root),
            workspace_path,
            "--watch=false",
            "--full",
        ]
        if profile:
            cmd.extend(["--profile", profile])
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"  SYNC FAILED: {result.stderr}")
            raise RuntimeError(f"databricks sync failed: {result.stderr}")
        print("  Sync complete.")
    finally:
        staging_handle.cleanup()
    return workspace_path


def deploy_app_code(
    ws: WorkspaceClient,
    app_name: str,
    workspace_path: str,
    dry_run: bool,
) -> None:
    """Deploy the app from the workspace source path."""
    source = f"/Workspace{workspace_path}"
    if dry_run:
        print(f"  [dry-run] Would deploy from {source}")
        return

    print(f"  Deploying from {source} ...")
    app_deployment = AppDeployment(
        source_code_path=source,
        mode=AppDeploymentMode.SNAPSHOT,
    )
    deployment = ws.apps.deploy_and_wait(
        app_name=app_name,
        app_deployment=app_deployment,
    )
    print(f"  Deployment {deployment.deployment_id} succeeded.")


# Every resource type the ``PATCH /api/2.0/apps/{name}`` body accepts.  An
# omission is not a no-op: ``build_app_resources`` drops the entry with a
# warning, and because the assembled list replaces the app's resources
# wholesale, the next deploy also removes a binding that was added by hand.
_RESOURCE_TYPE_KEYS = (
    "serving_endpoint",
    "sql_warehouse",
    "genie_space",
    "uc_securable",
    "secret",
    "job",
    "experiment",
    "postgres",
    "app",
    "database",
)


def build_app_resources(
    entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Convert YAML resource entries into raw dicts for the REST API.

    The Databricks Python SDK lags behind the REST API for UC securable
    types (TABLE, VIEW, etc.), so we build raw dicts that map 1:1 to the
    ``PATCH /api/2.0/apps/{name}`` body.
    """
    resources: list[dict[str, Any]] = []
    for entry in entries:
        name = entry.get("name")
        if not name:
            print("  WARNING: resource entry missing 'name', skipping")
            continue

        resource: dict[str, Any] = {"name": name}
        found = False
        for key in _RESOURCE_TYPE_KEYS:
            if key in entry:
                resource[key] = entry[key]
                found = True
                break

        if not found:
            print(f"  WARNING: unknown resource type in entry '{name}', skipping")
            continue

        if _has_unresolved_vars(resource):
            continue

        if "description" in entry:
            resource["description"] = entry["description"]
        resources.append(resource)
    return resources


def _has_unresolved_vars(obj: Any) -> bool:
    """Return True if any value still contains ``${var.*}`` placeholders."""
    if isinstance(obj, str):
        return "${var." in obj
    if isinstance(obj, dict):
        return any(_has_unresolved_vars(v) for v in obj.values())
    if isinstance(obj, list):
        return any(_has_unresolved_vars(v) for v in obj)
    return False


# Update mask sent to the async ``apps.create_update`` API for the
# core (non-UC) app spec.  ``create_update`` replaces whole collection
# fields, so listing ``resources`` / ``user_api_scopes`` here means the
# framework owns those collections in full on every redeploy.
_CORE_UPDATE_MASK = "compute_size,resources,user_api_scopes"
_TELEMETRY_UPDATE_FIELD = "telemetry_export_destinations"
_INSTANCES_UPDATE_MASK = "compute_min_instances,compute_max_instances"


def telemetry_export_destinations(destination: str | None) -> list[TelemetryExportDestination]:
    """Expand ``catalog.schema[.prefix]`` into the App's UC telemetry destination.

    Mirrors the App UI's catalog / schema / prefix form: tables are
    ``<prefix>_otel_logs``, ``<prefix>_otel_metrics`` and
    ``<prefix>_otel_spans`` (bare ``otel_*`` without a prefix).  An empty,
    ``None`` or still-unresolved (``${var.*}``) destination yields ``[]``,
    matching how :func:`build_app_resources` skips unresolved resources.
    """
    if not destination or _has_unresolved_vars(destination):
        return []
    parts = destination.split(".")
    if len(parts) not in (2, 3) or not all(parts):
        raise ValueError(
            f"telemetry_destination must be 'catalog.schema' or 'catalog.schema.prefix', got: {destination!r}"
        )
    base = f"{parts[0]}.{parts[1]}"
    table_prefix = f"{parts[2]}_" if len(parts) == 3 else ""
    return [
        TelemetryExportDestination(
            unity_catalog=UnityCatalog(
                logs_table=f"{base}.{table_prefix}otel_logs",
                metrics_table=f"{base}.{table_prefix}otel_metrics",
                traces_table=f"{base}.{table_prefix}otel_spans",
            )
        )
    ]


def _normalize_resource_dict(entry: dict[str, Any]) -> dict[str, Any]:
    """Return *entry* with the ``genie_space.id`` REST alias renamed to
    the SDK field ``space_id``.

    :meth:`AppResource.from_dict` reads ``space_id`` and would silently
    drop a genie binding authored with the REST/config name ``id``.
    Returns a shallow copy; the input dict is never mutated.
    """
    genie = entry.get("genie_space")
    if isinstance(genie, dict) and "id" in genie and "space_id" not in genie:
        new_genie = {k: v for k, v in genie.items() if k != "id"}
        new_genie["space_id"] = genie["id"]
        return {**entry, "genie_space": new_genie}
    return entry


def to_typed_resources(resources: list[dict[str, Any]]) -> list[AppResource]:
    """Convert REST-shaped resource dicts into typed ``AppResource`` models."""
    return [AppResource.from_dict(_normalize_resource_dict(r)) for r in resources]


def _build_app(
    app_name: str,
    description: str,
    compute_size: str,
    resources: list[dict[str, Any]],
    user_scopes: list[str],
    telemetry: list[TelemetryExportDestination] | None = None,
) -> App:
    """Assemble a typed :class:`App` spec from the resolved config."""
    return App(
        name=app_name,
        description=description,
        compute_size=ComputeSize(compute_size.upper()),
        resources=to_typed_resources(resources) or None,
        user_api_scopes=user_scopes or None,
        telemetry_export_destinations=telemetry or None,
    )


def scaling_steps(existing: App | None, instances: int) -> list[int]:
    """The instance counts to apply, in order, to reach *instances*.

    A standard App (no instance count yet) must convert to horizontal
    scaling at exactly 1 instance before it can scale further.
    """
    current_min = existing.compute_min_instances if existing else None
    current_max = existing.compute_max_instances if existing else None
    if current_min == instances and current_max == instances:
        return []
    if current_min is None and instances > 1:
        return [1, instances]
    return [instances]


def _reconcile_instances(ws: WorkspaceClient, app_name: str, existing: App | None, instances: int) -> None:
    steps = scaling_steps(existing, instances)
    if not steps:
        print(f"  Instance count already {instances}.")
        return
    for count in steps:
        print(f"  Setting instance count to {count} ...")
        app = App(name=app_name, compute_min_instances=count, compute_max_instances=count)
        ws.apps.create_update_and_wait(app_name, update_mask=_INSTANCES_UPDATE_MASK, app=app)


def reconcile_app_spec(
    ws: WorkspaceClient | None,
    app_name: str,
    description: str,
    resources: list[dict[str, Any]],
    user_scopes: list[str],
    compute_size: str,
    dry_run: bool,
    telemetry_destination: str | None = None,
    instances: int | None = None,
) -> None:
    """Reconcile the Databricks App *spec* (compute size, instance count,
    resources, OBO scopes, telemetry export) via the typed SDK -- creating the app when
    absent or updating it in place via the async ``create_update`` API
    otherwise.

    The spec is reconciled in phases so a failure binding UC securable
    resources (which require ``account users`` to hold ``USE CATALOG``)
    or the telemetry export cannot drop the core bindings -- LLM endpoint,
    warehouse, experiment, Lakebase -- that make the app functional:

    1. Core spec (``compute_size`` + non-UC resources + OBO scopes) --
       must succeed.  ``compute_size`` resize is supported here because
       ``create_update`` is the async update API the synchronous
       ``apps.update`` PATCH pointed callers to.
       Then the instance count, only when *instances* is set -- must
       succeed too.  Without it the scaling fields are never in an update
       mask, so a count set by hand in the App UI survives redeploys.
    2. UC securable resources -- best-effort; a failure leaves phase 1
       intact.
    3. Telemetry export -- best-effort, only when *telemetry_destination*
       is set, and after phase 2 so the SP already holds its grants on
       the ``otel_*`` tables.  Without a destination the field is never
       in an update mask, so a destination set by hand in the App UI
       survives redeploys.

    Note: app tagging (:func:`tag_app`) uses the Workspace Entity Tag
    Assignments API.  App tags do not propagate to billing; cost is
    tracked via ``usage_metadata.app_name`` in ``system.billing.usage``.
    """
    telemetry = telemetry_export_destinations(telemetry_destination)
    if dry_run:
        print(f"  [dry-run] Would set compute_size={compute_size} on {app_name}")
        if instances is not None:
            print(f"  [dry-run] Would set instance count to {instances} on {app_name}")
        if resources:
            print(f"  [dry-run] Would set {len(resources)} app resource(s) on {app_name}:")
            for r in resources:
                print(f"    - {r['name']}")
        if user_scopes:
            print(f"  [dry-run] Would set user_api_scopes on {app_name}: {user_scopes}")
        if telemetry:
            print(f"  [dry-run] Would export telemetry of {app_name} to {telemetry_destination}")
        return

    # Past the dry-run gate the caller always supplies a real client.
    assert ws is not None, "non-dry-run path requires a WorkspaceClient"

    core_resources = [r for r in resources if "uc_securable" not in r]
    uc_resources = [r for r in resources if "uc_securable" in r]

    core_app = _build_app(app_name, description, compute_size, core_resources, user_scopes)
    existing = None
    if app_exists(ws, app_name):
        existing = ws.apps.get(app_name)
        if _is_deleting(existing):
            print(f"  App '{app_name}' compute is DELETING; waiting until it is gone before create ...")
            if not wait_until_app_gone(ws, app_name):
                raise RuntimeError(
                    f"Cannot update app {app_name} as its compute is in DELETING state. "
                    "App compute needs to be ACTIVE or STOPPED to update."
                )
            existing = None
    if existing is not None:
        print(
            f"  Updating app spec (compute_size={compute_size}, "
            f"{len(core_resources)} core resource(s), {len(user_scopes)} OBO scope(s)) ..."
        )
        ws.apps.create_update_and_wait(app_name, update_mask=_CORE_UPDATE_MASK, app=core_app)
    else:
        print(
            f"  Creating app '{app_name}' (compute_size={compute_size}, "
            f"{len(core_resources)} core resource(s), {len(user_scopes)} OBO scope(s)) ..."
        )
        create_app_with_retry(ws, core_app)
    print("  Core app spec applied successfully.")
    if instances is not None:
        _reconcile_instances(ws, app_name, existing, instances)

    if uc_resources:
        full_app = _build_app(app_name, description, compute_size, core_resources + uc_resources, user_scopes)
        print(f"  Applying UC securable resources ({len(uc_resources)} additional) ...")
        try:
            ws.apps.create_update_and_wait(app_name, update_mask="resources", app=full_app)
            print("  All app resources applied successfully.")
        except Exception as exc:
            print(f"  WARNING: UC securable resources failed (core resources are intact): {exc}")
            print("  Hint: ensure 'account users' has USE CATALOG on the target catalog.")

    if telemetry:
        telemetry_app = _build_app(app_name, description, compute_size, resources, user_scopes, telemetry)
        print(f"  Applying telemetry export to {telemetry_destination} ...")
        try:
            ws.apps.create_update_and_wait(app_name, update_mask=_TELEMETRY_UPDATE_FIELD, app=telemetry_app)
            print("  Telemetry export applied successfully.")
        except Exception as exc:
            print(f"  WARNING: telemetry export failed (app spec is intact): {exc}")


def grant_sp_permissions(
    ws: WorkspaceClient | None,
    app_name: str,
    grants: list[dict[str, Any]],
    warehouse_id: str,
    dry_run: bool,
) -> None:
    """Grant UC permissions to the app's SP via SQL.

    The Apps resource API only supports a limited set of UC permissions
    (SELECT, MODIFY, ...).  Permissions like CREATE MODEL must be granted
    directly via SQL after the app (and its SP) exist.
    """
    if not grants:
        return

    if dry_run:
        for g in grants:
            print(
                f"  [dry-run] Would grant {g['permissions']} on {g['securable_type']} {g['securable_name']} to app SP"
            )
        return

    # Past the dry-run gate the caller always supplies a real client.
    assert ws is not None, "non-dry-run path requires a WorkspaceClient"

    try:
        app = ws.apps.get(app_name)
    except Exception as exc:
        print(f"  WARNING: cannot read app '{app_name}' for SP grants: {exc}")
        return

    sp_client_id = app.service_principal_client_id
    if not sp_client_id:
        print("  WARNING: app has no service_principal_client_id, skipping SP grants")
        return

    if not warehouse_id:
        print("  WARNING: no warehouse_id available, skipping SP grants")
        return

    for grant_entry in grants:
        securable_type = grant_entry.get("securable_type", "SCHEMA")
        securable_name = grant_entry.get("securable_name", "")
        permissions = grant_entry.get("permissions", [])
        if not securable_name or not permissions:
            continue

        grantee = grant_entry.get("grantee", f"`{sp_client_id}`")
        grantee_label = grantee if grantee != f"`{sp_client_id}`" else "app SP"

        for perm in permissions:
            statement = f"GRANT {perm} ON {securable_type} {quote_dotted_name(securable_name)} TO {grantee}"
            error = _run_grant(ws, warehouse_id, statement)
            if error is None:
                print(f"  Granted {perm} on {securable_type} {securable_name} to {grantee_label}")
            elif "already has" in error.lower():
                print(f"  {perm} on {securable_name}: already granted")
            else:
                print(f"  WARNING: GRANT failed ({perm} on {securable_name}): {error}")


def _run_grant(ws: WorkspaceClient, warehouse_id: str, statement: str) -> str | None:
    """Run a GRANT; return the error message, or ``None`` when it succeeded.

    A statement the warehouse rejects comes back as HTTP 200 with
    ``status.state == FAILED``, so the response is checked, not just the call.
    """
    try:
        response = ws.statement_execution.execute_statement(
            warehouse_id=warehouse_id,
            statement=statement,
            wait_timeout="30s",
        )
    except Exception as exc:
        return str(exc)
    status = response.status
    state = getattr(getattr(status, "state", None), "value", "")
    if state not in ("FAILED", "CANCELED", "CLOSED"):
        return None
    message = getattr(getattr(status, "error", None), "message", None)
    return message or f"statement {state}"


def tag_app(ws: WorkspaceClient | None, app_name: str, dry_run: bool) -> None:
    """Apply ai_solution_managed_by tag via the Workspace Entity Tag Assignments API."""
    if dry_run:
        print(f"  [dry-run] Would tag app '{app_name}' with {TAG_KEY_MANAGED_BY}={app_name}")
        return
    # Past the dry-run gate the caller always supplies a real client.
    assert ws is not None, "non-dry-run path requires a WorkspaceClient"
    try:
        from databricks.sdk.service.tags import TagAssignment

        ws.workspace_entity_tag_assignments.create_tag_assignment(
            TagAssignment(
                entity_type="apps",
                entity_id=app_name,
                tag_key=TAG_KEY_MANAGED_BY,
                tag_value=app_name,
            )
        )
        print(f"  Tag '{TAG_KEY_MANAGED_BY}' set on app '{app_name}'")
    except Exception as exc:
        if "already exists" in str(exc).lower():
            print(f"  Tag '{TAG_KEY_MANAGED_BY}' already set on app '{app_name}'")
        else:
            print(f"  WARNING: Could not tag app: {exc}")


def destroy_app(
    ws: WorkspaceClient | None,
    app_name: str,
    *,
    solution_root: str = "",
    dry_run: bool = False,
    timeout_seconds: float = 900,
    poll_seconds: float = 10,
) -> int:
    """Delete the Databricks App and its workspace sync directory.

    Generic teardown shared by every App payload.  Returns 0 on success
    (including the "app not found" no-op), 1 if the App delete failed.
    Workspace-dir cleanup failure is downgraded to a warning -- the App
    is already gone, and a stale source dir is harmless.

    ``ws`` may be ``None`` for the dry-run path; outside dry-run the
    caller always supplies a real client (asserted below).
    """
    workspace_path = f"{solution_root}/source" if solution_root else ""

    if dry_run:
        print(f"  [DRY RUN] Would delete app '{app_name}'")
        if workspace_path:
            print(f"  [DRY RUN] Would remove workspace dir {workspace_path}")
        return 0

    # Past the dry-run gate the caller always supplies a real client.
    assert ws is not None, "non-dry-run path requires a WorkspaceClient"

    if app_exists(ws, app_name):
        try:
            ws.apps.delete(name=app_name)
            print(f"  Deleted app '{app_name}'")
        except Exception as exc:
            print(f"  WARNING: Could not delete app '{app_name}': {exc}")
            return 1
        print(f"  Waiting for app '{app_name}' compute to leave DELETING ...")
        if not wait_until_app_gone(ws, app_name, timeout_seconds=timeout_seconds, poll_seconds=poll_seconds):
            print(
                f"  WARNING: app '{app_name}' still present after delete "
                f"(timeout {timeout_seconds:.0f}s); compute may still be DELETING"
            )
            return 1
    else:
        print(f"  App '{app_name}' not found — skipping delete.")

    if workspace_path:
        try:
            if clean_workspace_dir(ws, workspace_path, dry_run=False):
                print(f"  Removed workspace dir {workspace_path}")
        except Exception as exc:
            print(f"  WARNING: Could not clean workspace dir {workspace_path}: {exc}")

    return 0


# ---------------------------------------------------------------------------
# App URL state (issue #337)
# ---------------------------------------------------------------------------
#
# The deployed App's URL is an *infrastructure output* of the App host,
# so the engine owns publishing it as workspace-backed state -- separate
# from any payload-specific state (Lakebase chat history, UC connection).
# Persisted at the dedicated ``app`` brick-type path:
#
#     /Shared/agent-foundry/<instance>/_state/app/<instance>/<target>.json
#
# Any App payload (agent_app today, rest_mcp next) gets URL publication
# for free by calling :func:`publish_app_state` after a successful deploy.

APP_STATE_BRICK_TYPE = "app"


class AppHostState(BaseModel):
    """Workspace-backed state written by the App host after deploy.

    ``extra="ignore"`` so the framework ``_audit`` trail and any
    future-added keys do not break validation (issue #156 policy).
    """

    model_config = ConfigDict(extra="ignore")

    app_name: str
    app_url: str


def fetch_app_url(ws: WorkspaceClient, app_name: str) -> str | None:
    """Read the deployed App's public URL; ``None`` if unavailable."""
    try:
        app = ws.apps.get(app_name)
    except Exception as exc:
        print(f"  WARNING: could not read URL for app '{app_name}': {exc}")
        return None
    return app.url or None


def publish_app_state(
    ws: WorkspaceClient | None,
    instance_name: str,
    target: str,
    app_name: str,
    *,
    deployer: str = "",
    dry_run: bool = False,
) -> dict[str, str]:
    """Fetch the App URL and persist it as workspace-backed ``app`` state.

    Best-effort: a missing URL or a workspace write failure is logged,
    never raised, so it cannot fail an otherwise-successful deploy.
    Returns the published ``{app_name, app_url}`` dict (empty when the
    URL could not be resolved or in dry-run / offline mode).
    """
    if dry_run or ws is None:
        print(f"  [dry-run] Would publish App URL state for '{app_name}'")
        return {}

    url = fetch_app_url(ws, app_name)
    if not url:
        return {}

    from agent_foundry.state import save_state

    state: dict[str, str] = {"app_name": app_name, "app_url": url}
    save_state(
        ws,
        instance_name,
        APP_STATE_BRICK_TYPE,
        instance_name,
        target,
        dict(state),
        action="update",
        deployer=deployer,
        resource_id=app_name,
    )
    print(f"  Published App URL: {url}")
    return state


def load_app_state(
    ws: WorkspaceClient | None,
    instance_name: str,
    target: str,
) -> dict[str, str]:
    """Load the persisted ``{app_name, app_url}`` state (empty if absent)."""
    from agent_foundry.state import load_state

    raw = load_state(ws, instance_name, APP_STATE_BRICK_TYPE, instance_name, target)
    if not raw:
        return {}
    validate_app_state(raw)
    return {key: str(raw[key]) for key in ("app_name", "app_url") if key in raw}


def validate_app_state(raw: dict[str, Any]) -> None:
    """Validate persisted App state against :class:`AppHostState`."""
    from agent_foundry.state import validate_state

    validate_state(APP_STATE_BRICK_TYPE, raw, AppHostState)


def delete_app_state(
    ws: WorkspaceClient | None,
    instance_name: str,
    target: str,
) -> None:
    """Remove the persisted App URL state (teardown)."""
    from agent_foundry.state import delete_state

    delete_state(ws, instance_name, APP_STATE_BRICK_TYPE, instance_name, target)


# ---------------------------------------------------------------------------
# Generic deploy / destroy orchestration (issue #338)
# ---------------------------------------------------------------------------
#
# The fixed *sequence* of App-host primitives (stage+sync, upload app.yaml,
# reconcile the spec, grant SP permissions, deploy code, tag, publish URL)
# is identical for every payload.  Only the payload-specific inputs differ:
# the runtime ``AppPayloadSpec`` (command/env/extras), the neutral
# ``AppInfraSpec`` (compute/resources/scopes/grants) and an optional heal
# hook between reconcile and code-deploy.  These two functions own that
# sequence so a payload supplies the inputs via the ``AppPayloadAdapter``
# protocol and never re-implements the orchestration.


def deploy_on_host(
    payload: AppPayloadAdapter,
    *,
    ws: WorkspaceClient | None,
    app_name: str,
    target: str,
    variables: dict[str, str],
    instance_dir: Path | None,
    deployer: str,
    spec: Mapping[str, object] | None = None,
    dry_run: bool = False,
) -> int:
    """Run the generic Databricks App deploy sequence for *payload*.

    The payload supplies its inputs through the adapter protocol:
    :meth:`~agent_foundry.deploy.payload_adapters.AppPayloadAdapter.build_infra`
    (neutral infra), ``build_payload_spec`` (runtime contract) and
    ``after_reconcile`` (optional post-reconcile heal hook).  ``spec`` is
    the opaque ``payload.spec`` mapping the framework passes straight
    through to the adapter without interpretation.  Returns 0 on success,
    1 if any deploy step raised.
    """
    payload_spec = spec or {}
    infra = payload.build_infra(instance_dir, variables, target=target)
    app_spec = payload.build_payload_spec(instance_dir, variables, payload_spec)
    warehouse_id = variables.get("warehouse_id", "")
    sol_root = variables.get("solution_root", "")

    if dry_run:
        deployer = deployer or "<deployer>"
        ws_path_preview = f"{sol_root}/source" if sol_root else f"/Users/{deployer}/{app_name}"
        print(f"[DRY RUN] Would deploy app '{app_name}'")
        print(f"  Description: {infra.description}")
        print(f"  Workspace path: {ws_path_preview}")
        print(f"  Instance dir: {instance_dir}")
        print(f"  Target: {target}")
        print(f"  Compute size: {infra.compute_size}")
        upload_app_yaml(None, ws_path_preview, app_spec, dry_run=True)
        reconcile_app_spec(
            None,
            app_name,
            infra.description,
            infra.app_resources,
            infra.user_scopes,
            infra.compute_size,
            dry_run=True,
            telemetry_destination=infra.telemetry_destination,
            instances=infra.instances,
        )
        payload.after_reconcile(None, app_name, variables, dry_run=True)
        grant_sp_permissions(None, app_name, infra.sp_grants, warehouse_id, dry_run=True)
        tag_app(None, app_name, dry_run=True)
        instance_name = instance_dir.name if instance_dir else app_name
        publish_app_state(None, instance_name, target, app_name, dry_run=True)
        return 0

    # Past the dry-run early-return ``ws`` is always populated.
    assert ws is not None, "non-dry-run path requires a WorkspaceClient"

    try:
        profile = variables.get("profile")
        if instance_dir is None:
            raise RuntimeError("deploy_on_host() requires `instance_dir` for the wheel-based staging build (#112)")
        workspace_path = sync_source(
            ws,
            app_name,
            deployer,
            dry_run=False,
            instance_dir=instance_dir,
            solution_root=sol_root,
            profile=profile,
            extras=app_spec.requirements_extras,
            readme=resolve_payload_readme(payload, instance_dir),
        )
        upload_app_yaml(ws, workspace_path, app_spec, dry_run=False)
        reconcile_app_spec(
            ws,
            app_name,
            infra.description,
            infra.app_resources,
            infra.user_scopes,
            infra.compute_size,
            dry_run=False,
            telemetry_destination=infra.telemetry_destination,
            instances=infra.instances,
        )
        # Optional payload heal hook -- runs *after* the spec reconcile
        # (any rotated App SP / new role now exists) and *before* the code
        # deploy (first boot sees the healed state).  No-op for payloads
        # that do not need it.
        payload.after_reconcile(ws, app_name, variables, dry_run=False)
        grant_sp_permissions(ws, app_name, infra.sp_grants, warehouse_id, dry_run=False)
        deploy_app_code(ws, app_name, workspace_path, dry_run=False)
        tag_app(ws, app_name, dry_run=False)
        publish_app_state(ws, instance_dir.name, target, app_name, deployer=deployer)
    except Exception as exc:
        print(f"  DEPLOY FAILED: {format_sdk_error(exc)}")
        return 1

    print(f"  App '{app_name}' deployed successfully.")
    return 0


def destroy_on_host(
    ws: WorkspaceClient | None,
    app_name: str,
    target: str,
    *,
    instance_dir: Path | None = None,
    solution_root: str = "",
    dry_run: bool = False,
) -> int:
    """Tear down the App and its published URL state (generic teardown).

    Payload-specific teardown (e.g. Lakebase chat-history) is the
    payload's responsibility and is run by the caller around this; the
    host only owns deleting the App, its workspace dir and the App-URL
    state.
    """
    rc = destroy_app(ws, app_name, solution_root=solution_root, dry_run=dry_run)
    if not dry_run and instance_dir is not None:
        delete_app_state(ws, instance_dir.name, target)
    return rc
