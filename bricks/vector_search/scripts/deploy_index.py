"""
Vector Search Deploy Script
=============================
Manages the lifecycle of Vector Search endpoints and indexes using
the databricks-sdk WorkspaceClient.

Operations:
  - create:  Ensure endpoint exists, create the index
  - update:  Re-sync a Delta Sync index (triggers pipeline)
  - destroy: Delete the index (optionally the endpoint)
  - sync:    Trigger a sync for Delta Sync indexes

State management:
    After a successful create, the index details are written to the
    shared workspace state (agent_foundry.state, brick type
    ``vector_indexes``). On subsequent deploys, the script detects the
    existing index and uses sync instead of create.

Usage:
    python bricks/vector_search/scripts/deploy_index.py \\
        --instance <name> [--target dev] \\
        [--action create|destroy|sync] [--dry-run]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from agent_foundry import state as shared_state
from agent_foundry.bricks import fill_state_outputs, load_brick_variables, resolve_sub_instances
from agent_foundry.config import TAG_KEY_MANAGED_BY
from agent_foundry.loader import FoundryConfigError
from agent_foundry.variables import add_project_root_arg
from bricks.vector_search.scripts.validate_index import (
    load_config,
    substitute_variables,
    validate_index_config,
)

# ---------------------------------------------------------------------------
# State management (shared workspace state, keyed like VectorIndexesBrick reads it)
# ---------------------------------------------------------------------------

_BRICK_TYPE = "vector_indexes"


def _state_key(instance_dir: Path) -> tuple[str, str]:
    """``(app instance, sub-instance)`` for ``instances/<app>/vector_indexes/<sub>``."""
    return instance_dir.parent.parent.name, instance_dir.name


def _load_state(ws: Any | None, instance_dir: Path, target: str) -> dict[str, Any]:
    instance_name, sub_instance = _state_key(instance_dir)
    return shared_state.load_state(ws, instance_name, _BRICK_TYPE, sub_instance, target)


def _save_state(
    ws: Any | None,
    instance_dir: Path,
    target: str,
    state: dict[str, Any],
    *,
    action: str,
    deployer: str,
) -> None:
    instance_name, sub_instance = _state_key(instance_dir)
    shared_state.save_state(
        ws,
        instance_name,
        _BRICK_TYPE,
        sub_instance,
        target,
        state,
        action=action,
        deployer=deployer,
        resource_id=state.get("index_name", ""),
    )


def _delete_state(ws: Any | None, instance_dir: Path, target: str) -> None:
    instance_name, sub_instance = _state_key(instance_dir)
    shared_state.delete_state(ws, instance_name, _BRICK_TYPE, sub_instance, target)


# ---------------------------------------------------------------------------
# SDK helpers
# ---------------------------------------------------------------------------


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


def _ensure_endpoint(
    client: Any,
    endpoint_name: str,
    endpoint_type: str,
    dry_run: bool,
    *,
    budget_policy_id: str | None = None,
    target_qps: int | None = None,
    usage_policy_id: str | None = None,
) -> bool:
    """Ensure the VS endpoint exists. Returns True if it already existed.

    Optional endpoint knobs (``budget_policy_id`` / ``target_qps`` /
    ``usage_policy_id``) are only forwarded to the SDK when set, so the
    create call stays at the defaults otherwise.
    """
    try:
        ep = client.vector_search_endpoints.get_endpoint(endpoint_name)
        status = getattr(ep, "endpoint_status", None)
        state = getattr(status, "state", "UNKNOWN") if status else "UNKNOWN"
        print(f"  Endpoint '{endpoint_name}' exists (state={state})")
        return True
    except Exception:
        pass

    extras: dict[str, Any] = {}
    if budget_policy_id is not None:
        extras["budget_policy_id"] = budget_policy_id
    if target_qps is not None:
        extras["target_qps"] = target_qps
    if usage_policy_id is not None:
        extras["usage_policy_id"] = usage_policy_id

    if dry_run:
        suffix = f", {extras}" if extras else ""
        print(f"  [DRY RUN] Would create endpoint '{endpoint_name}' (type={endpoint_type}{suffix})")
        return False

    from databricks.sdk.service.vectorsearch import EndpointType

    print(f"  Creating endpoint '{endpoint_name}' (type={endpoint_type})...")
    client.vector_search_endpoints.create_endpoint_and_wait(
        name=endpoint_name,
        endpoint_type=EndpointType(endpoint_type),
        **extras,
    )
    print(f"  Endpoint '{endpoint_name}' created and ready")
    return False


def _tag_endpoint(
    client: Any,
    endpoint_name: str,
    tags: dict[str, str],
    dry_run: bool,
) -> None:
    """Apply custom tags to a Vector Search endpoint."""
    if not tags:
        return
    if dry_run:
        print(f"  [DRY RUN] Would tag endpoint '{endpoint_name}': {tags}")
        return
    try:
        from databricks.sdk.service.vectorsearch import CustomTag

        client.vector_search_endpoints.update_endpoint_custom_tags(
            endpoint_name=endpoint_name,
            custom_tags=[CustomTag(key=k, value=v) for k, v in tags.items()],
        )
        print(f"  Tagged endpoint '{endpoint_name}': {tags}")
    except Exception as exc:
        msg = str(exc)
        # Custom tags on VS endpoints are a known product limitation
        # (endpoints are typically shared; per-app tagging happens on
        # the index itself in ``_tag_index``).  Demote the WARNING so
        # deploy logs don't look like something is wrong.
        if "not supported" in msg.lower():
            print(
                "  Skipping endpoint tag (Vector Search product "
                "limitation): endpoints are not taggable; per-app "
                "tags live on the index"
            )
        else:
            print(f"  WARNING: failed to tag endpoint '{endpoint_name}': {exc}")


def _tag_index(
    client: Any,
    warehouse_id: str,
    index_name: str,
    tags: dict[str, str],
    dry_run: bool,
) -> None:
    """Apply UC tags to a Vector Search index via ALTER TABLE SET TAGS.

    The index is a UC securable so standard SQL tagging works.
    Failures are non-fatal (e.g. governed tag policies).
    """
    if not tags or not warehouse_id:
        return
    if dry_run:
        print(f"  [DRY RUN] Would tag index '{index_name}': {tags}")
        return
    tag_pairs = ", ".join(f"'{k}' = '{v}'" for k, v in tags.items())
    sql = f"ALTER TABLE `{'`.`'.join(index_name.split('.'))}` SET TAGS ({tag_pairs})"
    try:
        result = client.api_client.do(
            "POST",
            "/api/2.0/sql/statements",
            body={
                "warehouse_id": warehouse_id,
                "statement": sql,
                "wait_timeout": "30s",
            },
        )
        state = result.get("status", {}).get("state", "UNKNOWN")
        if state == "SUCCEEDED":
            print(f"  Tagged index '{index_name}': {tags}")
        else:
            msg = result.get("status", {}).get("error", {}).get("message", state)
            print(f"  WARNING: failed to tag index '{index_name}': {msg}")
    except Exception as exc:
        print(f"  WARNING: failed to tag index '{index_name}': {exc}")


def _build_delta_sync_spec(ds_config: dict[str, Any]) -> Any:
    """Build the DeltaSyncVectorIndexSpecRequest from config."""
    from databricks.sdk.service.vectorsearch import (
        DeltaSyncVectorIndexSpecRequest,
        EmbeddingSourceColumn,
        EmbeddingVectorColumn,
        PipelineType,
    )

    embedding_source = None
    if ds_config.get("embedding_source_columns"):
        embedding_source = [
            EmbeddingSourceColumn(
                name=col["name"],
                embedding_model_endpoint_name=col["embedding_model_endpoint_name"],
                model_endpoint_name_for_query=col.get("model_endpoint_name_for_query"),
            )
            for col in ds_config["embedding_source_columns"]
        ]

    embedding_vector = None
    if ds_config.get("embedding_vector_columns"):
        embedding_vector = [
            EmbeddingVectorColumn(
                name=col["name"],
                embedding_dimension=col["embedding_dimension"],
            )
            for col in ds_config["embedding_vector_columns"]
        ]

    pipeline_type_str = ds_config.get("pipeline_type", "TRIGGERED")
    pipeline_type = PipelineType(pipeline_type_str)

    return DeltaSyncVectorIndexSpecRequest(
        source_table=ds_config["source_table"],
        pipeline_type=pipeline_type,
        columns_to_sync=ds_config.get("columns_to_sync"),
        embedding_source_columns=embedding_source,
        embedding_vector_columns=embedding_vector,
        embedding_writeback_table=ds_config.get("embedding_writeback_table"),
    )


def _build_direct_access_spec(da_config: dict[str, Any]) -> Any:
    """Build the DirectAccessVectorIndexSpec from config."""
    from databricks.sdk.service.vectorsearch import (
        DirectAccessVectorIndexSpec,
        EmbeddingSourceColumn,
        EmbeddingVectorColumn,
    )

    embedding_source = None
    if da_config.get("embedding_source_columns"):
        embedding_source = [
            EmbeddingSourceColumn(
                name=col["name"],
                embedding_model_endpoint_name=col["embedding_model_endpoint_name"],
                model_endpoint_name_for_query=col.get("model_endpoint_name_for_query"),
            )
            for col in da_config["embedding_source_columns"]
        ]

    embedding_vector = None
    if da_config.get("embedding_vector_columns"):
        embedding_vector = [
            EmbeddingVectorColumn(
                name=col["name"],
                embedding_dimension=col["embedding_dimension"],
            )
            for col in da_config["embedding_vector_columns"]
        ]

    return DirectAccessVectorIndexSpec(
        schema_json=da_config.get("schema_json"),
        embedding_source_columns=embedding_source,
        embedding_vector_columns=embedding_vector,
    )


def _create_index(client: Any, config: dict[str, Any], dry_run: bool) -> dict[str, Any]:
    """Create a new Vector Search index. Returns state dict."""
    from databricks.sdk.service.vectorsearch import VectorIndexType

    index_cfg = config["index"]
    endpoint_name = config["endpoint"]["name"]
    index_name = index_cfg["name"]
    index_type_str = index_cfg["index_type"]

    if dry_run:
        print(f"  [DRY RUN] Would create index '{index_name}' (type={index_type_str})")
        return {"index_name": index_name, "endpoint_name": endpoint_name, "index_type": index_type_str}

    kwargs: dict[str, Any] = {
        "name": index_name,
        "endpoint_name": endpoint_name,
        "primary_key": index_cfg["primary_key"],
        "index_type": VectorIndexType(index_type_str),
    }

    if index_type_str == "DELTA_SYNC":
        kwargs["delta_sync_index_spec"] = _build_delta_sync_spec(index_cfg["delta_sync"])
    elif index_type_str == "DIRECT_ACCESS":
        kwargs["direct_access_index_spec"] = _build_direct_access_spec(index_cfg["direct_access"])

    print(f"  Creating index '{index_name}'...")
    try:
        result = client.vector_search_indexes.create_index(**kwargs)
    except Exception as exc:
        if "already exists" in str(exc).lower():
            print(f"  Index '{index_name}' already exists — adopting.")
            return {
                "index_name": index_name,
                "endpoint_name": endpoint_name,
                "index_type": index_type_str,
            }
        raise

    status = getattr(result, "status", None)
    ready = getattr(status, "ready", False) if status else False
    message = getattr(status, "message", "") if status else ""
    print(f"  Index created (ready={ready})")
    if message:
        print(f"  Status: {message}")

    return {
        "index_name": index_name,
        "endpoint_name": endpoint_name,
        "index_type": index_type_str,
    }


def _get_index_status(client: Any, index_name: str) -> dict[str, Any] | None:
    """Get current index status. Returns None if not found."""
    try:
        idx = client.vector_search_indexes.get_index(index_name)
        status = getattr(idx, "status", None)
        return {
            "ready": getattr(status, "ready", False) if status else False,
            "message": getattr(status, "message", "") if status else "",
            "indexed_row_count": getattr(status, "indexed_row_count", 0) if status else 0,
        }
    except Exception:
        return None


def _sync_index(client: Any, index_name: str, dry_run: bool) -> None:
    """Trigger a sync for a Delta Sync index."""
    if dry_run:
        print(f"  [DRY RUN] Would trigger sync for '{index_name}'")
        return

    print(f"  Triggering sync for '{index_name}'...")
    try:
        client.vector_search_indexes.sync_index(index_name)
        print("  Sync triggered")
    except Exception as exc:
        msg = str(exc)
        if "RUNNING" in msg:
            print("  Sync already running — skipping")
        # A ready index can still have a queued pipeline update (state
        # CREATED); that update reads the source table, so skip.
        elif "not ready to sync" in msg:
            print(f"  Pipeline update already pending — skipping ({msg})")
        else:
            raise


def _destroy_index(client: Any, index_name: str, dry_run: bool) -> None:
    """Delete a Vector Search index.

    Idempotent: treats "index already gone" as success so destroy
    survives partial / re-run scenarios (mirrors the MLflow experiment
    and warehouse-state-empty paths).
    """
    if dry_run:
        print(f"  [DRY RUN] Would delete index '{index_name}'")
        return

    print(f"  Deleting index '{index_name}'...")
    try:
        client.vector_search_indexes.delete_index(index_name)
        print("  Index deleted")
    except Exception as exc:
        msg = str(exc)
        not_found_markers = (
            "RESOURCE_DOES_NOT_EXIST",
            "does not exist",
            "not found",
        )
        if any(m in msg for m in not_found_markers):
            print(f"  Index '{index_name}' already gone -- treating as success.")
            return
        raise


def _destroy_action(
    instance_dir: Path,
    target: str,
    variables: dict[str, str],
    index_name: str,
    dry_run: bool,
    ws: Any | None,
) -> int:
    """Delete *index_name* and clear the instance state."""
    if dry_run:
        print(f"  [DRY RUN] Would destroy index '{index_name}'")
        return 0
    client = _get_client(variables, ws=ws)
    _destroy_index(client, index_name, dry_run=False)
    _delete_state(ws, instance_dir, target)
    return 0


# ---------------------------------------------------------------------------
# Main deploy function (called by orchestrator)
# ---------------------------------------------------------------------------


def deploy(
    instance_dir: Path,
    target: str,
    variables: dict[str, str],
    action: str | None = None,
    dry_run: bool = False,
    *,
    ws: Any | None = None,
) -> int:
    """Deploy a Vector Search index instance.

    Args:
        instance_dir: Path to the instance directory containing index_config.yml.
        target: Deployment target (dev, staging, prod).
        variables: Variable substitutions.
        action: Explicit action (create, destroy, sync). None = auto-detect.
        dry_run: If True, validate and print what would happen.

    Returns:
        0 on success, non-zero on error.
    """
    config_path = instance_dir / "index_config.yml"
    if not config_path.exists():
        print(f"  ERROR: {config_path} not found")
        return 1

    config = load_config(config_path)
    config = substitute_variables(config, variables)
    state = _load_state(ws, instance_dir, target)
    deployer = variables.get("deployer", "")

    # The deployed index name is all destroy needs; create-only vars may be gone.
    if action == "destroy" and state.get("index_name"):
        return _destroy_action(instance_dir, target, variables, state["index_name"], dry_run, ws)

    try:
        validate_index_config(config, source=config_path)
    except FoundryConfigError as exc:
        print(f"  Validation FAILED: {exc}")
        return 1

    if action == "destroy":
        return _destroy_action(instance_dir, target, variables, config["index"]["name"], dry_run, ws)

    if action == "sync":
        index_name = state.get("index_name") or config["index"]["name"]
        if not dry_run:
            client = _get_client(variables, ws=ws)
            _sync_index(client, index_name, dry_run=False)
        else:
            print(f"  [DRY RUN] Would sync index '{index_name}'")
        return 0

    # Auto-detect: create or sync
    endpoint_cfg = config["endpoint"]
    app_tags = {TAG_KEY_MANAGED_BY: variables["app_name"]} if variables.get("app_name") else {}
    endpoint_opts = {
        "budget_policy_id": endpoint_cfg.get("budget_policy_id"),
        "target_qps": endpoint_cfg.get("target_qps"),
        "usage_policy_id": endpoint_cfg.get("usage_policy_id"),
    }
    if not dry_run:
        client = _get_client(variables, ws=ws)
        _ensure_endpoint(
            client,
            endpoint_cfg["name"],
            endpoint_cfg.get("endpoint_type", "STANDARD"),
            dry_run,
            **endpoint_opts,
        )
        _tag_endpoint(client, endpoint_cfg["name"], app_tags, dry_run)
    else:
        _ensure_endpoint(
            None,
            endpoint_cfg["name"],
            endpoint_cfg.get("endpoint_type", "STANDARD"),
            dry_run,
            **endpoint_opts,
        )
        _tag_endpoint(None, endpoint_cfg["name"], app_tags, dry_run)

    warehouse_id = variables.get("warehouse_id", "")

    if state.get("index_name"):
        index_name = state["index_name"]
        if not dry_run:
            status = _get_index_status(client, index_name)
            if status:
                print(f"  Index '{index_name}' exists (ready={status['ready']}, rows={status['indexed_row_count']})")
                _tag_index(client, warehouse_id, index_name, app_tags, dry_run)
                # Only trigger an explicit sync once the index has
                # finished provisioning.  A fresh DELTA_SYNC index
                # auto-syncs from its source table when it first
                # reaches READY, so posting a manual sync beforehand
                # returns ``BadRequest: not ready`` and crashes the
                # deploy on first-run workspaces.
                if config["index"]["index_type"] == "DELTA_SYNC":
                    if status["ready"]:
                        _sync_index(client, index_name, dry_run)
                    else:
                        print(
                            f"  Index '{index_name}' still provisioning; "
                            "initial sync will happen automatically -- "
                            "skipping explicit sync"
                        )
                return 0
            else:
                config_index_name = config["index"]["name"]
                if config_index_name != index_name:
                    config_status = _get_index_status(client, config_index_name)
                    if config_status:
                        print(f"  Stale state; adopting existing '{config_index_name}'")
                        new_state = {**state, "index_name": config_index_name}
                        _save_state(ws, instance_dir, target, new_state, action="adopt", deployer=deployer)
                        _tag_index(client, warehouse_id, config_index_name, app_tags, dry_run)
                        if config["index"]["index_type"] == "DELTA_SYNC":
                            _sync_index(client, config_index_name, dry_run)
                        return 0
                print(f"  Index '{index_name}' not found in workspace, recreating...")
        else:
            print(f"  [DRY RUN] Index '{index_name}' found in state, would check and sync")
            _tag_index(None, warehouse_id, index_name, app_tags, dry_run)
            return 0

    if not dry_run:
        new_state = _create_index(client, config, dry_run)
        _save_state(ws, instance_dir, target, new_state, action="create", deployer=deployer)
        created_index = new_state.get("index_name", "")
        if created_index:
            _tag_index(client, warehouse_id, created_index, app_tags, dry_run)
    else:
        _create_index(None, config, dry_run=True)

    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deploy a Vector Search index")
    parser.add_argument(
        "--instance",
        required=True,
        help="Path to the vector index directory (absolute or relative to project root), "
        "e.g. instances/<your-app>/vector_indexes/<index-name>",
    )
    parser.add_argument("--target", default="dev", help="Deployment target")
    parser.add_argument("--action", choices=["create", "destroy", "sync"], help="Explicit action")
    parser.add_argument("--dry-run", action="store_true", help="Validate only, don't call APIs")
    parser.add_argument("--vars", nargs="*", default=[], help="Variables as key=value")
    add_project_root_arg(parser)
    args = parser.parse_args(argv)

    root = args.project_root
    try:
        instance_dir, index_dirs = resolve_sub_instances(root, args.instance, "vector_indexes")
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    variables = load_brick_variables(root, args.target, instance_dir, extra_vars=args.vars)
    ws = _get_client(variables, construct=True)
    variables = fill_state_outputs(variables, instance_dir, args.target, ws)

    failures = 0
    for index_dir in index_dirs:
        failures += deploy(index_dir, args.target, variables, action=args.action, dry_run=args.dry_run, ws=ws)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
