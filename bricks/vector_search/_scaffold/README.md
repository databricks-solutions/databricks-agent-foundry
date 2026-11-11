# Vector Search Index Instance

This directory defines a single Vector Search index. Customize the files below and deploy.

## Files

| File | Purpose |
|------|---------|
| `index_config.yml` | Endpoint, index type, source table, embedding configuration |
| `benchmarks/queries.yml` | Test queries to validate retrieval quality after deployment |

## Quick Start

```bash
# 1. Copy this template
cp -r bricks/vector_search/_scaffold/ instances/my_app/vector_indexes/my_docs/

# 2. Edit index_config.yml with your settings

# 3. Validate
uv run agent-foundry brick vector_search validate-index --instance my_docs \
  --vars catalog=my_catalog --vars schema=my_schema

# 4. Deploy
uv run agent-foundry brick vector_search deploy-index --instance my_docs --target dev \
  --vars catalog=my_catalog --vars schema=my_schema --vars vs_endpoint_name=my_endpoint
```

## Index Types

### Delta Sync (recommended for most use cases)

Auto-syncs with a source Delta table. Databricks manages the embedding pipeline.

**Requirements:**
- Source table must have Change Data Feed enabled
- For managed embeddings: a Foundation Model API endpoint (e.g., `databricks-gte-large-en`)
- `USE CATALOG` and `USE SCHEMA` on the target location

**Configuration:** Set `index_type: "DELTA_SYNC"` and fill the `delta_sync:` section.

### Citation URIs

To return links to original documents, add a `doc_uri` column to the source
Delta table and include it in `delta_sync.columns_to_sync`. A Unity Catalog
Volume URI can include a fragment for a specific location:

```text
/Volumes/my_catalog/my_schema/source_docs/handbook.pptx#slide=12
```

For managed AI Search MCP tools, request `doc_uri` through
`tools.default_meta.columns` in the MCP server's `server.yml`.

### Direct Access

You manage embeddings and data updates yourself via REST/SDK.

**Requirements:**
- A schema definition for the index
- Pre-computed embedding vectors or a model endpoint

**Configuration:** Set `index_type: "DIRECT_ACCESS"` and fill the `direct_access:` section.

## Variable Substitution

Use `${var.<name>}` in YAML values. Variables are resolved from:
1. `[targets.<target>]` in the repo-root `foundry.toml` (workspace: `catalog`, `schema`, ...)
2. `instances/<name>/foundry.toml` (instance identity: `name`, `schema_prefix`, ...)
3. `--vars key=value` CLI arguments (highest priority)

Common variables:
- `catalog` -- Unity Catalog catalog name
- `schema` -- Schema for the index and source table
- `vs_endpoint_name` -- Shared Vector Search endpoint name (not instance-scoped; indexes are)
- `index_name` -- Index name (combined with catalog.schema)
- `source_table` -- Name of the source Delta table
