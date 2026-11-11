"""
Vector Search Index Export
============================
Exports an existing Vector Search index into the decomposed
index_config.yml file format for use with this brick.

Usage:
    python bricks/vector_search/scripts/export_index.py \\
        --index-name catalog.schema.my_index \\
        --output instances/my_app/vector_indexes/my_index/
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import yaml


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


def export_index(index_name: str, output_dir: Path) -> None:
    """Export an existing Vector Search index to the template file format."""
    client = _get_client(construct=True)
    if client is None:
        raise RuntimeError("workspace client required to export a vector search index")

    print(f"Fetching index '{index_name}'...")
    idx = client.vector_search_indexes.get_index(index_name)

    endpoint_name = idx.endpoint_name or ""
    index_type = idx.index_type.value if idx.index_type else "DELTA_SYNC"
    primary_key = idx.primary_key or ""

    config: dict[str, Any] = {
        "endpoint": {
            "name": endpoint_name,
            "endpoint_type": "STANDARD",
        },
        "index": {
            "name": idx.name or index_name,
            "primary_key": primary_key,
            "index_type": index_type,
        },
    }

    if index_type == "DELTA_SYNC" and idx.delta_sync_index_spec:
        spec = idx.delta_sync_index_spec
        ds: dict[str, Any] = {}
        if spec.source_table:
            ds["source_table"] = spec.source_table
        if spec.pipeline_type:
            ds["pipeline_type"] = spec.pipeline_type.value
        if spec.embedding_source_columns:
            ds["embedding_source_columns"] = [_export_embedding_source(col) for col in spec.embedding_source_columns]
        if spec.embedding_vector_columns:
            ds["embedding_vector_columns"] = [_export_embedding_vector(col) for col in spec.embedding_vector_columns]
        if spec.embedding_writeback_table:
            ds["embedding_writeback_table"] = spec.embedding_writeback_table
        config["index"]["delta_sync"] = ds

    elif index_type == "DIRECT_ACCESS" and idx.direct_access_index_spec:
        spec = idx.direct_access_index_spec
        da: dict[str, Any] = {}
        if spec.schema_json:
            da["schema_json"] = spec.schema_json
        if spec.embedding_source_columns:
            da["embedding_source_columns"] = [_export_embedding_source(col) for col in spec.embedding_source_columns]
        if spec.embedding_vector_columns:
            da["embedding_vector_columns"] = [_export_embedding_vector(col) for col in spec.embedding_vector_columns]
        config["index"]["direct_access"] = da

    # Try to get endpoint type
    try:
        ep = client.vector_search_endpoints.get_endpoint(endpoint_name)
        if ep.endpoint_type:
            config["endpoint"]["endpoint_type"] = ep.endpoint_type.value
    except Exception:
        pass

    output_dir.mkdir(parents=True, exist_ok=True)
    config_path = output_dir / "index_config.yml"

    with open(config_path, "w", encoding="utf-8") as f:
        f.write("# Exported from existing Vector Search index\n")
        f.write(f"# Source: {index_name}\n\n")
        yaml.dump(config, f, default_flow_style=False, sort_keys=False, allow_unicode=True)

    print(f"Exported to {config_path}")

    # Create empty benchmarks dir
    benchmarks_dir = output_dir / "benchmarks"
    benchmarks_dir.mkdir(exist_ok=True)
    queries_path = benchmarks_dir / "queries.yml"
    if not queries_path.exists():
        with open(queries_path, "w", encoding="utf-8") as f:
            f.write("# Add benchmark queries for this index\nqueries: []\n")

    status = getattr(idx, "status", None)
    if status:
        ready = getattr(status, "ready", False)
        rows = getattr(status, "indexed_row_count", 0)
        print(f"Index status: ready={ready}, indexed_rows={rows}")


def _export_embedding_source(col: Any) -> dict[str, str]:
    result: dict[str, str] = {}
    if col.name:
        result["name"] = col.name
    if col.embedding_model_endpoint_name:
        result["embedding_model_endpoint_name"] = col.embedding_model_endpoint_name
    if col.model_endpoint_name_for_query:
        result["model_endpoint_name_for_query"] = col.model_endpoint_name_for_query
    return result


def _export_embedding_vector(col: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if col.name:
        result["name"] = col.name
    if col.embedding_dimension:
        result["embedding_dimension"] = col.embedding_dimension
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export a Vector Search index to template format")
    parser.add_argument("--index-name", required=True, help="Fully qualified index name (catalog.schema.index)")
    parser.add_argument("--output", required=True, help="Output directory for the exported instance")
    args = parser.parse_args(argv)

    export_index(args.index_name, Path(args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
