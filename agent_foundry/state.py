"""
Workspace-Backed State Management
===================================
Shared state persistence for bricks that need to track remote resource IDs
(Genie Spaces, Dashboards) across deploys, machines, and CI runs.

The workspace is the **single source of truth**.  ``save_state`` writes
only to the workspace and never writes a local cache, eliminating the risk
that stale or per-fork local files override the live workspace state at
deploy time or get bundled into deployed app source via ``databricks sync``
(see issue #50).

For dry-run / offline modes (``ws=None``), ``load_state`` returns ``{}`` and
``save_state`` only records the audit entry on the in-memory ``state`` dict.

Each state file includes an ``_audit`` trail — an append-only list of
deploy events (who, when, what action, which resource ID).

Workspace path convention::

    /Shared/agent-foundry/<app_name>/_state/<brick_type>/<sub_instance>/<target>.json

    Examples:
        /Shared/agent-foundry/<your-app>/_state/genie_spaces/info_schema/dev.json
        /Shared/agent-foundry/<your-app>/_state/dashboard/<your-app>/dev.json

Note: State paths are target-independent (multi-target isolation is via
the ``<target>.json`` filename).  Deployed resources use a target-partitioned
root: ``/Shared/agent-foundry/<target>/<app_name>/``.
"""

from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from typing import Any, cast

from pydantic import BaseModel, ValidationError

_SOLUTION_ROOT_PREFIX = "/Shared/agent-foundry"
_MAX_AUDIT_ENTRIES = 50


# ---------------------------------------------------------------------------
# State-schema contract enforcement (issue #156)
# ---------------------------------------------------------------------------


class BrickStateValidationError(ValueError):
    """Raised when a brick's persisted state file violates its declared
    ``state_schema``.

    The exception message names the offending brick and lists every
    missing required field plus a Pydantic-formatted explanation, so
    operators can repair the state file (or recognise that they are
    deploying onto state written by a newer / older framework version).
    """


def validate_state(
    brick_name: str,
    state: dict[str, Any],
    schema: type[BaseModel] | None,
    *,
    sub_instance: str | None = None,
) -> None:
    """Validate a state dict against a brick's declared ``state_schema``.

    Issue #156: bricks may declare a Pydantic model in their
    ``state_schema`` class attribute describing the shape of their
    persisted workspace state file.  When set, every read of that state
    file is validated against the schema, so contract violations surface as a clear
    :class:`BrickStateValidationError` instead of a delayed
    ``KeyError`` deep in a downstream brick.

    Parameters
    ----------
    brick_name
        Stable brick identifier (used in the error message).
    state
        Raw state dict, typically the result of ``json.loads(state_file)``.
        Audit-trail fields (``_audit``) and other framework metadata
        live alongside the brick-owned keys; the schema is responsible
        for declaring or tolerating them.
    schema
        Optional Pydantic model.  ``None`` is a no-op so bricks that
        have not yet declared a schema keep the legacy permissive
        behaviour (this is the default for every brick except
        ``warehouse`` until follow-up issues land).
    sub_instance
        Optional sub-instance name for nested-layout bricks
        (genie_spaces, vector_indexes, mcp_servers, ...).  Included in
        the error message so operators can pinpoint the offending file.

    Raises
    ------
    BrickStateValidationError
        When ``schema`` is set and ``state`` does not validate.  The
        original :class:`pydantic.ValidationError` is chained so its
        per-field detail is preserved on ``__cause__``.

    Notes
    -----
    Schema policy: bricks should set ``model_config = ConfigDict(extra="ignore")``
    so optional / framework-metadata keys (e.g. workspace ``_audit``
    trails, future-version-added fields) don't break validation.  The
    contract is "the brick declares which keys it owns and downstream
    bricks can rely on", not "the state file is byte-exact".
    """
    if schema is None:
        return
    try:
        schema.model_validate(state)
    except ValidationError as exc:
        location = f"{brick_name}[{sub_instance!r}]" if sub_instance else brick_name
        missing = [".".join(str(loc) for loc in err["loc"]) for err in exc.errors() if err["type"] == "missing"]
        msg_parts = [
            f"State file for brick {location} violates its declared state_schema ({schema.__name__}).",
        ]
        if missing:
            msg_parts.append(f"Missing required field(s): {', '.join(missing)}.")
        msg_parts.append(f"Pydantic detail: {exc.errors(include_url=False)!r}")
        raise BrickStateValidationError(" ".join(msg_parts)) from exc


def _ws_state_path(
    instance_name: str,
    brick_type: str,
    sub_instance: str,
    target: str,
) -> str:
    return f"{_SOLUTION_ROOT_PREFIX}/{instance_name}/_state/{brick_type}/{sub_instance}/{target}.json"


def _read_workspace_file(ws: Any, path: str) -> dict[str, Any] | None:
    """Read a JSON file from the workspace.  Returns None if not found."""
    import base64

    try:
        resp = ws.workspace.export(path)
        content = resp.content
        if content is None:
            return None
        if isinstance(content, bytes):
            raw = content.decode("utf-8")
        else:
            raw = content
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            decoded = json.loads(base64.b64decode(raw).decode("utf-8"))
        # ``json.loads`` returns ``Any`` per the stubs; we trust the
        # shape because the workspace file format is owned by this
        # module (see ``_write_workspace_file`` above).
        return cast("dict[str, Any] | None", decoded)
    except Exception:
        return None


def _write_workspace_file(ws: Any, path: str, data: dict[str, Any]) -> None:
    """Write a JSON file to the workspace, creating parent dirs as needed."""
    from databricks.sdk.service.workspace import ImportFormat

    parent = path.rsplit("/", 1)[0]
    ws.workspace.mkdirs(parent)
    ws.workspace.upload(
        path=path,
        content=io.BytesIO(json.dumps(data, indent=2).encode("utf-8")),
        format=ImportFormat.AUTO,
        overwrite=True,
    )


def _append_audit(
    state: dict[str, Any],
    action: str,
    deployer: str,
    resource_id: str,
) -> None:
    """Append an audit entry to the state dict (in-place)."""
    audit: list[dict[str, str]] = state.setdefault("_audit", [])
    audit.append(
        {
            "action": action,
            "deployer": deployer,
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "resource_id": resource_id,
        }
    )
    if len(audit) > _MAX_AUDIT_ENTRIES:
        state["_audit"] = audit[-_MAX_AUDIT_ENTRIES:]


def load_state(
    ws: Any | None,
    instance_name: str,
    brick_type: str,
    sub_instance: str,
    target: str,
) -> dict[str, Any]:
    """Load state from the workspace; ``{}`` when absent or offline (``ws=None``).

    Args:
        ws: WorkspaceClient (None for dry-run/offline).
        instance_name: Application instance name (e.g. "<your-app>").
        brick_type: Brick category (e.g. "genie_spaces", "dashboard").
        sub_instance: Sub-instance name (e.g. "info_schema", or instance_name for dashboard).
        target: Deploy target (e.g. "dev").
    """
    if ws is None:
        return {}
    return _read_workspace_file(ws, _ws_state_path(instance_name, brick_type, sub_instance, target)) or {}


def save_state(
    ws: Any | None,
    instance_name: str,
    brick_type: str,
    sub_instance: str,
    target: str,
    state: dict[str, Any],
    *,
    action: str = "update",
    deployer: str = "",
    resource_id: str = "",
) -> None:
    """Save state to the workspace with an audit entry.

    The workspace is the single source of truth; nothing is written
    locally (issue #50).  When ``ws`` is ``None`` (dry-run/offline) this
    function only mutates ``state`` in-memory with the audit entry.

    Args:
        ws: WorkspaceClient (None for dry-run/offline — no persistence).
        instance_name: Application instance name.
        brick_type: Brick category.
        sub_instance: Sub-instance name.
        target: Deploy target.
        state: State dict (top-level keys like space_id, dashboard_id).
        action: Audit action (create, update, recreate, destroy).
        deployer: Email of the deployer.
        resource_id: Primary resource identifier for the audit entry.
    """
    if deployer or resource_id:
        existing: dict[str, Any] = {}
        if ws is not None:
            ws_path = _ws_state_path(instance_name, brick_type, sub_instance, target)
            existing = _read_workspace_file(ws, ws_path) or {}
        if "_audit" in existing:
            state["_audit"] = existing["_audit"]
        _append_audit(state, action, deployer, resource_id)

    if ws is not None:
        ws_path = _ws_state_path(instance_name, brick_type, sub_instance, target)
        try:
            _write_workspace_file(ws, ws_path, state)
        except Exception as exc:
            print(f"  WARNING: Could not write state to workspace: {exc}")


def delete_state(
    ws: Any | None,
    instance_name: str,
    brick_type: str,
    sub_instance: str,
    target: str,
) -> None:
    """Remove state from the workspace."""
    if ws is not None:
        ws_path = _ws_state_path(instance_name, brick_type, sub_instance, target)
        try:
            ws.workspace.delete(ws_path)
        except Exception:
            pass
