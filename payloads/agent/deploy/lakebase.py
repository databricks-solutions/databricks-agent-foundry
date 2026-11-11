"""
Lakebase Autoscaling provisioning -- internal helper for the Agent App brick
=============================================================================
Provisions a Lakebase Autoscaling project that backs the Agent App's
``postgres`` ``app_resource`` for persistent chat history + feedback
storage.  The ``e2e-chatbot-app-next`` UI template auto-connects when
the project is bound as an app resource.

A new project auto-creates a ``production`` branch with a
``databricks_postgres`` database.  This module discovers the
auto-generated database resource ID required by the Apps resource API.

History
-------
This module is the relocated successor of
``bricks/lakebase/scripts/deploy_lakebase.py``.  The standalone
``LakebaseBrick`` was removed: chat history is a *capability* of the
Agent App, not a peer brick.  Earlier issues that shaped today's
contract:

- #18, #46, #56, #57 -- SP-rotation / schema-ownership healing history.
- #149 -- brick CLI unification: this helper has no standalone
  ``main()`` / ``argparse`` entrypoint.  It is called only from
  :class:`bricks.app.brick.AppBrick.deploy`.
- #175 -- the :class:`LakebaseState` schema declared below.
- #191 (EPIC, F-PR6) -- typed config plumbing.
- #338 -- chat history became an agent-payload capability; the toggle
  now lives in ``spec.ui.chat_history.enabled`` and is read via
  ``chat_history_enabled(ctx)`` (the deploy variable bag), not off the
  framework ``InstanceConfig``.

SP rotation
-----------
The previous ``sp_client_id`` is tracked in state and a rotation is
logged for diagnostics.  The Agent App's declared ``postgres``
``app_resource`` (permission ``CAN_CONNECT_AND_CREATE``) provisions the
new SP's Postgres role when the App deploys, but that role cannot
access the ``drizzle`` / ``ai_chatbot`` schemas owned by the *previous*
SP.  :func:`ensure_app_sp_lakebase_access` heals this by granting the
new role ``DATABRICKS_SUPERUSER`` membership through the Lakebase role
API -- platform-native and data-preserving, replacing the old manual
``REASSIGN OWNED BY`` recovery.  It is called from the Agent App deploy
after the App spec is reconciled (role exists) and before the App code
is deployed (first-boot migration sees the elevated privileges).

State lives in the shared workspace state (:mod:`agent_foundry.state`,
type ``lakebase``) so any machine can redeploy or destroy.

This is an **upsert** -- safe to run repeatedly.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

from agent_foundry import state as shared_state

if TYPE_CHECKING:
    from databricks.sdk import WorkspaceClient

STATE_TYPE = "lakebase"


class LakebaseState(BaseModel):
    """Contract for the shared ``lakebase`` workspace state (#175).

    Written by :func:`provision_lakebase_project` as a single document.
    ``lakebase_database_path`` may be an empty string when the database
    resource path could not be discovered -- still a string, still
    required.

    ``sp_client_id`` is declared optional: the current provisioning
    function always writes it, but state files written before the
    SP-rotation feature was added did not -- making it required would
    gratuitously break ``load_state_outputs`` for any instance whose
    last deploy pre-dates that change.  The Agent App reads its SP
    from its own ``app_resource`` (not from this state), so the field
    is also intentionally NOT exposed via ``state_keys`` on
    :class:`bricks.app.brick.AppBrick`; declaring it here
    keeps the schema honest about the on-disk shape.

    ``extra="ignore"`` covers future-version metadata.
    """

    model_config = ConfigDict(extra="ignore")

    lakebase_project_name: str
    lakebase_branch_path: str
    lakebase_database_path: str
    sp_client_id: str | None = None


def _get_client(
    variables: dict[str, str],
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


def _default_project_name(instance_dir: Path, variables: dict[str, str]) -> str:
    target_name = variables.get("target_name", "dev")
    app_name = variables.get("app_name", instance_dir.name)
    return f"{target_name}-{app_name}-lakebase"


def load_state(ws: Any | None, instance_dir: Path, target: str) -> dict[str, Any]:
    name = instance_dir.name
    return shared_state.load_state(ws, name, STATE_TYPE, name, target)


def _save_state(ws: Any | None, instance_dir: Path, target: str, state: dict[str, str], *, deployer: str) -> None:
    name = instance_dir.name
    shared_state.save_state(
        ws,
        name,
        STATE_TYPE,
        name,
        target,
        dict(state),
        action="update",
        deployer=deployer,
        resource_id=state.get("lakebase_project_name", ""),
    )


def _delete_state(ws: Any | None, instance_dir: Path, target: str) -> None:
    name = instance_dir.name
    shared_state.delete_state(ws, name, STATE_TYPE, name, target)


def _discover_database_path(api: Any, project_name: str) -> str | None:
    """List databases on the production branch and return the first
    resource path.

    The database resource path (``projects/.../databases/db-xxxx-...``)
    is auto-generated and cannot be predicted ahead of time.
    """
    branch_path = f"projects/{project_name}/branches/production"
    try:
        resp = api.do("GET", f"/api/2.0/postgres/{branch_path}/databases")
        databases = resp.get("databases", [])
        if databases:
            name = databases[0].get("name", "")
            return str(name) if name else ""
    except Exception as exc:
        print(f"  WARNING: Could not list databases: {exc}")
    return None


def _get_app_sp_client_id(ws: WorkspaceClient, app_name: str) -> str | None:
    """Look up the current Service Principal client ID for a Databricks App."""
    try:
        app = ws.apps.get(app_name)
        return app.service_principal_client_id or None
    except Exception:
        return None


# Prefix the platform assigns to an App service principal's auto-created
# Lakebase role: ``dbrx-apps-<service_principal_client_id>``.
_SP_LAKEBASE_ROLE_PREFIX = "dbrx-apps-"


def ensure_app_sp_lakebase_access(
    ws: WorkspaceClient | None,
    app_name: str,
    variables: dict[str, str],
    *,
    dry_run: bool = False,
) -> None:
    """Heal chat-history schema access for the App's *current* SP role.

    The Agent App's ``postgres`` ``app_resource``
    (``CAN_CONNECT_AND_CREATE``) materialises a Postgres role for the
    App's service principal when the App deploys, but that role owns
    *only* objects it creates.  After an SP rotation the new role cannot
    touch the ``drizzle`` / ``ai_chatbot`` schemas owned by the previous
    SP, so the chat UI fails with ``permission denied for schema``.

    The platform-native, data-preserving remedy is to grant the new
    role ``DATABRICKS_SUPERUSER`` membership through the Lakebase role
    API -- the only membership the API exposes.  No manual SQL, no
    ``REASSIGN OWNED BY`` ownership surgery, no data loss.

    Must run *after* the App spec is reconciled (so the SP role exists)
    and *before* the App's code is deployed (so the chat UI's first-boot
    migration sees the elevated privileges).  Best-effort and
    idempotent: a not-yet-materialised role or any API error is
    downgraded to a warning -- the chat UI's migration step is itself
    non-fatal, so the deploy must never block on this heal.
    """
    branch_path = variables.get("lakebase_branch_path", "")
    if not branch_path:
        return

    if dry_run:
        print("  [DRY RUN] Would ensure App SP has DATABRICKS_SUPERUSER on the Lakebase branch")
        return

    if ws is None:
        return

    sp_client_id = _get_app_sp_client_id(ws, app_name)
    if not sp_client_id:
        print("  WARNING: App has no service_principal_client_id; skipping Lakebase access heal")
        return

    role_name = f"{branch_path}/roles/{_SP_LAKEBASE_ROLE_PREFIX}{sp_client_id}"

    from databricks.sdk.common.types.fieldmask import FieldMask
    from databricks.sdk.service.postgres import Role, RoleMembershipRole, RoleRoleSpec

    pg: Any = ws.postgres
    try:
        role = pg.get_role(name=role_name)
    except Exception as exc:
        print(f"  WARNING: App SP Lakebase role not found yet ({role_name}); skipping access heal ({exc})")
        return

    current = (role.status.membership_roles if role.status else None) or []
    if RoleMembershipRole.DATABRICKS_SUPERUSER in current:
        print("  App SP already has DATABRICKS_SUPERUSER on Lakebase (chat-history schema access OK)")
        return

    try:
        pg.update_role(
            name=role_name,
            role=Role(spec=RoleRoleSpec(membership_roles=[RoleMembershipRole.DATABRICKS_SUPERUSER])),
            update_mask=FieldMask(field_mask=["spec.membership_roles"]),  # type: ignore[no-untyped-call]
        ).wait()
        print("  Granted DATABRICKS_SUPERUSER to App SP on Lakebase (heals chat-history schema access)")
    except Exception as exc:
        print(f"  WARNING: could not grant Lakebase membership to App SP: {exc}")


def provision_lakebase_project(
    instance_dir: Path,
    target: str,
    variables: dict[str, str],
    dry_run: bool = False,
    *,
    ws: Any | None = None,
) -> int:
    """Ensure a Lakebase Autoscaling project exists. Returns 0 on success.

    Called from :class:`bricks.app.brick.AppBrick.deploy`
    when chat history is enabled (``spec.ui.chat_history.enabled``,
    surfaced via ``chat_history_enabled(ctx)``).  Mutates
    *variables* in place to publish the discovered project / branch /
    database paths so downstream substitutions in
    ``app_resources`` (the ``postgres`` resource) resolve at deploy
    time.

    SP rotation is detected via state diff and logged for diagnostics;
    the Agent App's declared ``postgres`` ``app_resource`` handles the
    actual role + permission provisioning when the App deploys.
    """
    project_name = variables.get("lakebase_project_name", "") or _default_project_name(instance_dir, variables)

    if dry_run:
        print(f"  [DRY RUN] Would ensure Lakebase project '{project_name}'")
        return 0

    ws = _get_client(variables, ws=ws)
    if ws is None:
        print("  ERROR: workspace client required to provision Lakebase")
        return 1
    pg: Any = ws.postgres

    project_path = f"projects/{project_name}"
    try:
        project = pg.get_project(name=project_path)
    except Exception as exc:
        exc_str = str(exc).lower()
        if "not found" not in exc_str and "does not exist" not in exc_str:
            print(f"  ERROR checking project: {exc}")
            return 1

        print(f"  Creating Lakebase project '{project_name}'...")
        try:
            from databricks.sdk.service.postgres import Project

            op = pg.create_project(project=Project(), project_id=project_name)
            op.wait()
            print(f"  Project '{project_name}' is ready")
        except Exception as create_exc:
            if "already exists" in str(create_exc).lower():
                print(f"  Project '{project_name}' already exists -- adopting.")
            else:
                print(f"  ERROR creating project: {create_exc}")
                return 1
    else:
        # ``delete_project`` (our destroy path) soft-deletes: the project
        # lingers with ``delete_time`` set until a 7-day purge, and the
        # name stays reserved so a plain create fails.  ``get_project``
        # still returns it, so a destroy->redeploy would otherwise log
        # "exists", skip create, then fail database discovery (the branch
        # is gone) and silently drop chat history.  Restore it instead --
        # reuses the same project and preserves prior chat-history data.
        if getattr(project, "delete_time", None) is not None:
            print(f"  Lakebase project '{project_name}' is soft-deleted (pending purge); restoring (undelete)...")
            try:
                pg.undelete_project(name=project_path).wait()
                print(f"  Lakebase project '{project_name}' restored (chat-history data preserved)")
            except Exception as undelete_exc:
                print(f"  ERROR restoring soft-deleted project '{project_name}': {undelete_exc}")
                return 1
        else:
            print(f"  Lakebase project '{project_name}' exists")

    branch_path = f"projects/{project_name}/branches/production"

    # --- SP rotation detection ---
    prev_state = load_state(ws, instance_dir, target)
    prev_sp = prev_state.get("sp_client_id", "")
    app_name = variables.get("app_name", instance_dir.name)
    target_name = variables.get("target_name", "dev")
    full_app_name = variables.get("full_app_name", f"{target_name}-{app_name}-app")
    current_sp = _get_app_sp_client_id(ws, full_app_name) or ""

    sp_rotated = prev_sp and current_sp and prev_sp != current_sp
    if sp_rotated:
        print(
            f"  SP rotation detected ({prev_sp[:8]}... -> {current_sp[:8]}...); "
            "Agent App deploy will re-provision the new SP's Postgres role"
        )
    database_path = _discover_database_path(ws.api_client, project_name) or ""

    if not database_path:
        print(
            "  WARNING: Could not discover database resource path "
            "(postgres app_resource will be omitted from the Agent App deploy)"
        )

    print(f"  Branch: {branch_path}")
    print(f"  Database: {database_path}")

    # Persist freshly-discovered project / branch / database paths and
    # the current SP *before* any downstream call.  The Agent App
    # consumes these via state; persisting up front ensures a later
    # failure does not leave readers looking at stale paths from a
    # previous deploy.
    variables["lakebase_project_name"] = project_name
    variables["lakebase_branch_path"] = branch_path
    variables["lakebase_database_path"] = database_path
    _save_state(
        ws,
        instance_dir,
        target,
        {
            "lakebase_project_name": project_name,
            "lakebase_branch_path": branch_path,
            "lakebase_database_path": database_path,
            "sp_client_id": current_sp,
        },
        deployer=variables.get("deployer", ""),
    )

    # SP rotation is surfaced for diagnostics only; the Agent App
    # handles the actual provisioning via its ``postgres``
    # ``app_resource``.  If an SP rotation ever leaves app-owned
    # schemas orphaned on the old role, the manual recovery is a
    # single ``REASSIGN OWNED BY <old_sp> TO <new_sp>`` as the
    # database owner.
    return 0


def lakebase_project_name(
    instance_dir: Path,
    target: str,
    variables: dict[str, str],
    *,
    ws: Any | None = None,
) -> str:
    """Project name ``destroy_lakebase_project`` deletes: state, then variables, then the default."""
    project_name = load_state(ws, instance_dir, target).get("lakebase_project_name") or variables.get(
        "lakebase_project_name", ""
    )
    return str(project_name) if project_name else _default_project_name(instance_dir, variables)


def destroy_lakebase_project(
    instance_dir: Path,
    target: str,
    variables: dict[str, str],
    *,
    dry_run: bool = False,
    ws: Any | None = None,
) -> int:
    """Delete the Lakebase Autoscaling project recorded in state."""
    project_name = lakebase_project_name(instance_dir, target, variables, ws=ws)

    if dry_run:
        print(f"  [DRY RUN] Would delete Lakebase project '{project_name}'")
        return 0

    ws = _get_client(variables, ws=ws)
    if ws is None:
        print("  ERROR: workspace client required to destroy Lakebase")
        return 1
    project_path = f"projects/{project_name}"
    try:
        ws.postgres.delete_project(name=project_path)
        print(f"  Deleted Lakebase project '{project_name}'")
    except Exception as exc:
        exc_str = str(exc).lower()
        if "not found" in exc_str or "does not exist" in exc_str:
            print(f"  Lakebase project '{project_name}' not found — skipping.")
        else:
            print(f"  ERROR deleting Lakebase project '{project_name}': {exc}")
            return 1

    _delete_state(ws, instance_dir, target)
    return 0


__all__ = [
    "LakebaseState",
    "destroy_lakebase_project",
    "ensure_app_sp_lakebase_access",
    "lakebase_project_name",
    "load_state",
    "provision_lakebase_project",
]
