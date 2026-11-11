# Brick Reference

> Per-brick summary. Each brick's single authored doc is its `README.md`; this
> page is the cross-cutting index over the bricks that ship in the wheel.

| Brick | Purpose | Doc |
|-------|---------|-----|
| `ai_gateway` | Git-managed Unity Gateway artifacts (skills) | [README](../../bricks/ai_gateway/README.md) |
| `app` | Databricks App host (chat UI, optional MCP/A2A server) | [README](../../bricks/app/README.md) |
| `tracing` | Provisions the UC-backed MLflow experiment for tracing | [README](../../bricks/tracing/README.md) |
| `data` | Curated views, materialized tables, and metadata in Unity Catalog | [README](../../bricks/data/README.md) |
| `evaluation` | Scorer catalog, eval runner, and production monitoring | [README](../../bricks/evaluation/README.md) |
| `genie` | Genie Space infrastructure-as-code | [README](../../bricks/genie/README.md) |
| `job` | Databricks Jobs for scheduled and file-arrival workflows | [README](../../bricks/job/README.md) |
| `mcp` | Declarative MCP server registry (agent-payload capability) | [README](../../payloads/agent/deploy/mcp/README.md) |
| `model_serving` | Serverless Model Serving endpoints for UC models (CPU or GPU, optional build job) | [README](../../bricks/model_serving/README.md) |
| `observability` | Lakeview operational dashboard | [README](../../bricks/observability/README.md) |
| `skills` | Skill definitions and system-prompt assembly (agent-payload capability) | [README](../../payloads/agent/deploy/skills/README.md) |
| `sql_function` | Unity Catalog function tools | [README](../../bricks/sql_function/README.md) |
| `vector_search` | Vector Search endpoints/indexes for RAG | [README](../../bricks/vector_search/README.md) |
| `warehouse` | Dedicated tagged SQL warehouse provisioning | [README](../../bricks/warehouse/README.md) |
