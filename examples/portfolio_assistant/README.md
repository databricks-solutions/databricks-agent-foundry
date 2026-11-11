<p align="center">
  <img src="assets/portfolio-assistant-icon.png" alt="Portfolio Assistant" width="360">
</p>

# Portfolio Assistant — an end-to-end production agent

Most agent examples stop at a chat UI calling an LLM. A production agent
needs much more around it: curated and governed data, document retrieval
with an ingestion pipeline, metadata that stays fresh, tracing, evaluation
before and after release, production monitoring, an operations dashboard,
identity and permissions, and a deploy path across `dev`, `staging`, and
`prod`. Built by hand, that is weeks of glue code, and every team builds it
differently.

This example ships all of it as one instance. Pick `portfolio_assistant` in
`agent-foundry init`, run `agent-foundry deploy`, and every part below is
provisioned in your workspace, wired together, and covered by tests.

## What the agent does

A data assistant over Unity Catalog. Users ask about tables, columns, and
schemas (`system.information_schema`) and about the documentation corpus;
the agent answers with SQL results, charts, and citations back to the
source file. Other agents can call it over A2A or MCP.

## What's in the box

| Concern | What the example deploys | Where |
|---------|--------------------------|-------|
| **Agent App** | Chat UI, agent with four skills (`catalog-explorer`, `sql-runner`, `documentation-explorer`, `chart-renderer`), Lakebase chat history, per-message feedback, A2A and MCP endpoints | `app/`, `payloads/agent/` |
| **Governed tools** | Four MCP servers (Genie, Vector Search, UC functions, SQL warehouse) behind a fail-closed allowlist | `payloads/agent/mcp_servers/` |
| **Curated data** | Views over `information_schema` with Unity Catalog comments | `data_domains/catalog_explorer/` |
| **Analytics** | Genie space with instructions, SQL examples, join specs, and benchmark questions | `genie_spaces/info_schema/` |
| **Document retrieval** | UC volume → file-arrival ingestion job → Delta table → Vector Search index, with citations to the source file | `data_domains/docs_corpus/`, `jobs/docs_ingest/`, `vector_indexes/docs_search/` |
| **Metadata management** | Scheduled profiling job rebuilds landscape tables; `get_data_landscape()` feeds the agent's data universe into the cached prompt | `jobs/data_universe_profile/`, `sql_functions/data_universe/`, `data_domains/data_universe/` |
| **Compute** | Dedicated SQL warehouse (or reuse an existing one) | `warehouse/`, `foundry.toml` |
| **Tracing & experiment tracking** | Agent spans, App telemetry, and user feedback written directly to Unity Catalog OTel tables (spans, logs, metrics, annotations); the MLflow experiment and the dashboard read from them | `tracing` brick (via `payload.requires`) |
| **Evaluation** | Golden set, built-in scorers plus custom ones (tool routing, Genie SQL correctness, response quality), per-skill eval questions | `evaluation/`, `payloads/agent/skills/*/eval/` |
| **Production monitoring** | Registered scorers sampling 50% of live traces | `evaluation/eval_config.yml` |
| **Operations dashboard** | Lakeview dashboard: volume, latency, errors, tools, quality, feedback, users, token and cache economics | `observability` brick (via `payload.requires`) |
| **Cost control** | Prompt caching and tool-result clearing | `payloads/agent/payload.yml` |
| **Identity & permissions** | On-behalf-of scopes, service principal grants, App resources | `app/config.yml` |
| **Tests** | Instance unit tests; post-deploy smoke questions over `/invocations`, MCP, and A2A | `tests/`, `smoke.yml` |

## The lifecycle it covers

1. **Build:** `agent-foundry init` generates the repo with this instance.
2. **Deploy:** `agent-foundry deploy --target dev` provisions every brick.
3. **Verify:** `agent-foundry smoke` probes the live App; `agent-foundry eval`
   scores it against the golden set.
4. **Release:** CI promotes to `staging` and `prod` with approval gates and
   acceptance tests.
5. **Operate:** `agent-foundry monitors` registers production scorers; the
   dashboard and human feedback show quality and cost.
6. **Improve:** traces and feedback point to the next golden-set questions
   and skill changes.

## Architecture

The example runs on the built-in **`agent` payload** inside the generic `app` host. The agent loop gets its instructions from a cached system prompt (`system_prompt`, skills, and the data universe) and its tools from MCP servers mounted next to it. Skills tell the agent *when and how* to use tools; they do not proxy them. Every tool call passes the fail-closed **MCP allowlist**, the single auditable place that decides which tools the agent can ever invoke. The cost and behaviour trade-offs of the payload's toggles (prompt caching, skill loading, the data universe) are covered in [`payloads/agent/README.md`](payloads/agent/README.md). Each box below is provided by a composable **brick**; include only the ones you need.

<p align="center">
  <img src="assets/architecture.svg" alt="Portfolio Assistant architecture" width="100%">
</p>

This example uses four managed MCP servers; external MCP servers and custom MCP Apps attach the same way, behind the same allowlist. A minimal instance might use only the `agent` payload + `app` + one MCP server; the orchestrator (`agent-foundry deploy`) deploys whichever bricks are present.

For how the host itself stays payload-agnostic and how payloads are swapped, see the [framework architecture](../../docs/explanation/architecture.md).

## Naming Convention

All Databricks resource names follow the pattern `[target] solution-name-brick-name`
(display names) or `target-solution-name-brick-name` (API resource names).
Names are auto-computed by `InstanceConfig` from `app_name` + `target_name` --
instance configs only contain instance-specific overrides.

| Resource | Example (dev) |
|----------|---------------|
| Warehouse | `[dev] portfolio-assistant-warehouse` |
| Genie Space | `[dev] portfolio-assistant-genie-info-schema` |
| Dashboard | `[dev] portfolio-assistant-dashboard` |
| Docs ingest job | `dev-portfolio-assistant-docs-ingest` |
| App | `dev-portfolio-assistant-app` |
| Chat UI | `dev-portfolio-assistant-app-ui` |
| Chat history (Lakebase) | `dev-portfolio-assistant-lakebase` -- provisioned by the `app` brick when `spec.ui.chat_history.enabled = true` (payload.yml) |
| MLflow experiment | `/Shared/agent-foundry/dev/portfolio-assistant/experiments/portfolio-assistant-traces` |
| UC schemas | `{catalog}.portfolio_assistant_views`, `_indexes`, `_agent` |

## Configuration Contract

Instance configs hold only instance-specific values. Auto-computed fields
(resource names, tracing paths, workspace paths) are injected by brick
deploy scripts from `InstanceConfig.to_variables()`.

Each config file uses inline safety markers:
- `SAFE TO CHANGE` -- deployer can freely modify
- `CHANGE WITH CARE` -- requires coordinated changes across files listed below

### Cross-File Dependencies

| Concern | Files that must agree |
|---------|----------------------|
| **LLM endpoint** | `payloads/agent/payload.yml` `spec.runtime.model.name`, `app/config.yml` `llm-endpoint` serving endpoint name |
| **Genie space** | `payloads/agent/mcp_servers/catalog_genie/server.yml` reads `${var.genie_space_id__info_schema}`, published by `genie_spaces/info_schema/`; skills name the MCP tool (`catalog_genie__query_space`), never the space ID. |
| **Vector index** | `payloads/agent/mcp_servers/docs_search/server.yml` catalog and schema <-> where `vector_indexes/docs_search/` deploys the index; skills name the MCP tool (`docs_search__docs_index`), never the index FQN. |
| **Vector index + columns** | `vector_indexes/.../index_config.yml` `columns_to_sync`, `payloads/agent/mcp_servers/docs_search/server.yml` `tools.default_meta.columns` |
| **Docs ingestion** | `jobs/docs_ingest/job.yml` watches the volume created by `data_domains/docs_corpus` and passes `${var.vs_index_name__docs_search}` to the ingestion notebook. |
| **Domain name** | `data_domains/catalog_explorer/domain.yml` `domain.name`, `genie_spaces/.../space.yml` `variables.domain`, `data_sources/tables.yml` identifiers |
| **App name / schema prefix** | `foundry.toml` `app_name` + `schema_prefix` -- referenced by other files via `${var.*}` |

### What's Safe to Change

| Setting | File | Notes |
|---------|------|-------|
| LLM model | `payloads/agent/payload.yml` `spec.runtime.model.name` | Also update `app/config.yml` endpoint name |
| System prompt | `payloads/agent/payload.yml` `spec.runtime.system_prompt` | Free to customize |
| Prompt caching, skill loading, data universe | `payloads/agent/payload.yml` `spec.runtime.context` / `tools.skills` | Interacting cost trade-offs -- see [`payloads/agent/README.md`](payloads/agent/README.md) |
| Max tool calls / iterations | `payloads/agent/payload.yml` `spec.runtime` | Tune agent behavior |
| Tracing on/off | (always on) | Tracing is always enabled in deployed Apps; to disable, call `mlflow.tracing.disable()` from a startup hook (see `payloads/agent/runtime/tracing.py`) |
| Input/output redaction | `app/app.yaml` | `MLFLOW_TRACE_REDACT_INPUTS` / `MLFLOW_TRACE_REDACT_OUTPUTS` env vars |
| Warehouse mode | `foundry.toml` `[warehouse] mode` | `"dedicated"` (default; provisions a new warehouse) or `"reuse"` (binds to an existing `warehouse_id`) |
| Warehouse size | `warehouse/config.yml` | Affects cost and performance (dedicated mode only) |
| Warehouse auto-stop | `warehouse/config.yml` | Idle timeout (dedicated mode only) |
| Request timeout | `app/app.yaml` | `CHAT_PROXY_TIMEOUT_SECONDS` |
| Scorer lists | `evaluation/eval_config.yml` | Add/remove evaluation scorers |
| Eval sample rate | `evaluation/eval_config.yml` | Production monitoring frequency |
| Retrieval tuning | `payloads/agent/mcp_servers/docs_search/server.yml` `tools.default_meta` | Retrieval settings |
| OBO scopes | `app/config.yml` | Add scopes when adding tools |

### Source-document citations

The documentation corpus demonstrates the full citation path:

1. `data_domains/docs_corpus/volumes/00_docs_sources.sql` creates the Unity
   Catalog volume before dependent tables.
2. `jobs/docs_ingest` watches that volume, upserts uploaded text files into
   the docs table, and triggers the Delta Sync index.
3. `tables/01_docs.sql` stores each source file's `/Volumes/...` path in
   `doc_uri`.
4. `vector_indexes/docs_search/index_config.yml` syncs `doc_uri`.
5. `payloads/agent/mcp_servers/docs_search/server.yml` includes `doc_uri` in
   AI Search MCP `tools.default_meta.columns`.

## File Layout

```
instances/portfolio-assistant/
  foundry.toml                    # Instance identity (name, app_name, schema_prefix, [warehouse])
  app/config.yml                  # Generic host: app resources (SP), OBO scopes, grants, payload ref
  payloads/agent/payload.yml      # Payload adapter + spec: the single config contract
                                  #   (spec.runtime LLM/prompt/limits/tools, spec.ui toggles, spec.integrations a2a/mcp_server)
  payloads/agent/skills/          # One <skill-id>/SKILL.md per skill, plus eval questions
  payloads/agent/mcp_servers/     # MCP server registry (per-server tools.allow allowlist)
  evaluation/eval_config.yml      # Scorer config and monitoring
  evaluation/datasets/            # Golden set for evaluation
  warehouse/config.yml            # SQL warehouse sizing (dedicated mode only; name auto-computed)
  data_domains/                   # View definitions for Genie
  genie_spaces/                   # Genie Space IaC (title auto-computed)
  vector_indexes/                 # Vector Search index definitions
  jobs/docs_ingest/               # File-arrival docs ingestion job
  jobs/data_universe_profile/     # Scheduled profiling of curated views (data universe)
  sql_functions/catalog_utils/    # Unity Catalog SQL function tools
  sql_functions/data_universe/    # get_data_landscape() / get_table_metadata()
  smoke.yml                       # Post-deploy smoke questions (agent-foundry smoke)
  assets/                         # README icon and architecture diagram
  tests/                          # Instance unit tests
```
