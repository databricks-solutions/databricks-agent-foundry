# Data Domain Template

Curated views, materialized tables, and metadata deployed to Unity Catalog. Copy this template to `instances/<app>/data_domains/<your_domain>/` and customize. Supports both computed views (for Genie Spaces) and Delta tables (for Vector Search or standalone use).

## Quick Start

```bash
# 1. Copy template to a new instance
cp -r bricks/data/_scaffold instances/my_app/data_domains/my_domain

# 2. Edit domain.yml with your target catalog/schema
# 3. Add CREATE VOLUME statements to volumes/ when source files are needed
# 4. Add view/table SQL and metadata

# 5. Deploy
uv run agent-foundry brick data deploy --instance my_domain --target dev --dry-run
uv run agent-foundry brick data deploy --instance my_domain --target dev
```

## Choosing a Data Model Approach

```
Start here
  │
  ├─ Do you need a materialized Delta table (e.g., for Vector Search)?
  │   YES → Materialized Table (tables/)
  │         Enable Change Data Feed for Delta Sync indexes
  │   NO  ↓
  │
  ├─ Do users ask the same aggregated KPIs repeatedly?
  │   YES → Pre-Aggregated Views (views/pre_aggregated/)
  │   NO  ↓
  │
  ├─ Do you need governed, certified metric definitions?
  │   YES → Metric Views (views/metric_views/)
  │         ⚠ Cannot mix with regular tables in one Genie Space
  │   NO  ↓
  │
  ├─ Are users querying data that spans multiple source tables?
  │   YES → Denormalized Views (views/denormalized/) ← RECOMMENDED DEFAULT
  │   NO  ↓
  │
  └─ Simple single-table use case
      → Attach source table directly to Genie Space (no view needed)
```

### Approach Comparison

| Approach | When to Use | Downstream Consumer | Effort |
|----------|------------|---------------------|--------|
| **Denormalized View** | Most use cases. Pre-join related tables. | Genie Space | Low |
| **Pre-Aggregated View** | Dashboards, repeated rollups | Genie Space | Medium |
| **Metric View** | Strict KPI governance (finance, exec reporting) | Genie Space | Higher |
| **Materialized Table** | Vector Search source, synthetic data, ETL output | Vector Search, any | Medium |
| **Direct Tables** | Single clean table, no joins needed | Genie Space | None |

## Scaffold Structure

```
_scaffold/
├── README.md                                  # This file
├── domain.yml                                 # Default catalog/schema config
├── volumes/
│   └── 00_example_sources.sql                 # UC source-document volume
├── views/
│   ├── denormalized/
│   │   └── 01_catalog_objects.sql             # Pre-joined flat view
│   ├── pre_aggregated/
│   │   └── 01_schema_summary.sql             # Rolled-up KPIs
│   └── metric_views/
│       └── 01_catalog_metrics.sql            # UC metric view (YAML body)
├── tables/
│   └── 01_docs_corpus.sql                    # Materialized Delta table with CDF
└── metadata/
    ├── 01_comments.sql                        # COMMENT ON TABLE/COLUMN
    └── 02_tags.sql                            # UC tags for governance
```

This template is **deployable as-is** using `system.information_schema` as the data source. Just provide `${var.catalog}` and `${var.warehouse_id}`. `domain.yml` deploys into schema `${var.schema_prefix}` so two instances in the same catalog do not share a schema.

## Instance Layout

When you create an instance from this template, use `volumes/` for source-document storage, `views/` for CREATE VIEW statements, and `tables/` for CREATE TABLE + INSERT patterns. Deployment runs phases in dependency order: volumes, views, tables, then metadata. Files are sorted by path within each phase.

```
instances/<app>/data_domains/<your_domain>/
├── domain.yml                    # Target catalog, schema, source filters
├── volumes/                      # UC volumes (optional, executed first)
│   └── 00_<volume_name>.sql
├── views/                        # Computed views (optional)
│   ├── 01_<view_name>.sql       # Executed first
│   ├── 02_<view_name>.sql       # Executed second (can reference 01)
│   └── ...
├── tables/                       # Materialized Delta tables (optional)
│   └── 01_<table_name>.sql      # CREATE TABLE + INSERT (multi-statement OK)
└── metadata/
    └── comments.sql              # COMMENT ON for all views and tables
```

A domain can use any combination of `volumes/`, `views/`, and `tables/`.
For citation links, place source files in the volume and store their
`/Volumes/<catalog>/<schema>/<volume>/<file>` paths in a Delta table
`doc_uri` column. Add a fragment such as `#slide=12` when the UI should link
to a specific slide.

## domain.yml Reference

```yaml
domain:
  name: catalog_explorer                      # Human-readable domain name
  catalog: "${var.catalog}"                   # Resolved from the target in foundry.toml
  schema: "${var.schema_prefix}_views"        # Convention: {schema_prefix}_{purpose}
  source_filter:                              # Optional: document where source data lives
    table_catalog: "system"
    table_schema: "information_schema"
```

## Variable Substitution

All config and SQL files use the `${var.name}` syntax (same convention as Genie and Vector Search bricks). Variables are resolved from:

1. `[targets.<target>]` in the repo-root `foundry.toml` (workspace: `catalog`, `schema`, ...)
2. `instances/<name>/foundry.toml` (instance identity: `name`, `schema_prefix`, ...)
3. `domain.yml` `catalog` and `schema` fields (override the global values for SQL execution)
4. `--vars key=value` CLI arguments (highest priority)

## Schema Convention

Schemas follow the pattern `{schema_prefix}_{purpose}`:

- `{prefix}_views` -- all Genie Space views (domain-prefixed names for uniqueness)
- `{prefix}_indexes` -- source tables and Vector Search indexes
- `{prefix}_agent` -- MLflow traces, evaluation datasets, logs

This convention:
- Groups resources by instance (`schema_prefix`) and purpose
- Makes permissions simple (`GRANT USE SCHEMA + SELECT`)
- Scales to multiple Genie Spaces sharing the same `_views` schema
- The deploy script creates schemas automatically if they don't exist
