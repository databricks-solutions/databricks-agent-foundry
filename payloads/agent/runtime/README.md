# payloads/agent/runtime/

> **Technical reference** for deployers and developers.

The agent loop (OpenAI Agents SDK). Skills are assembled into the system prompt; direct (in-process) tools are wired from skills only when `mcp_only: false`. MCP-served tools are wired declaratively via the [`mcp`](../deploy/mcp/README.md) capability (per-instance `mcp_servers/<name>/server.yml`). The agent reads the capability's workspace state (`/Shared/agent-foundry/<instance>/_state/mcp_servers/`) at startup and attaches each as a `FilteredMCPServerStreamableHttp` (allow-list, prompts/resources consumption).

Request lifecycle and diagram: [How it works inside](../README.md#how-it-works-inside).

## Contents

| File | Purpose |
|------|---------|
| `agent.py` | OpenAI Agents SDK orchestrator with `invoke` / `stream` handlers |
| `core.py` | `create_agent` / `model_settings_for`; re-exports `create_tools`, `get_workspace_client`, `load_agent_config_and_vars` for instances |
| `brick_mcp_servers.py` | `build_mcp_servers` (reads `mcp` capability state, constructs `FilteredMCPServerStreamableHttp`) |
| `tool_builders.py` | Direct, skill and python tool factories (`create_tools`, `create_skill_tools`, `create_python_tools`) |
| `runtime_config.py` | Variables, Agents SDK init and the cached `load_agent_config_and_vars` |
| `request_context.py` | Request state, forwarded identity headers and workspace clients (SP, OBO, `resolve_tools_client`) |
| `instance_paths.py` | Active instance dir and project root |
| `instructions.py` | Instruction assembly and the time/identity preamble |
| `prompt_registry.py` | MLflow Prompt Registry registration and deployer lookup |
| `responses_input.py` | Responses-input repair before `Runner.run` |
| `mcp_wrapper.py` | Allow-list / glob / alias / prefix + auto-poll layered on `MCPServerStreamableHttp` |
| `mcp_consumption.py` | Per-request `prompts` (tool manuals) + `resources` (synthetic `<server>__read_resource` tool) |
| `config.py` | Default settings + `load_agent_config()` for instance overrides |

## Key Design Decision

The agent does NOT hardcode tool names or URLs. At startup it:
1. Resolves the active instance directory (`INSTANCE_DIR` env var or auto-detect)
2. Builds direct tools from assembled **skills** (`SKILL_TOOLS` -- Genie / Vector Search / UC Function function-call shape)
3. Attaches MCP servers from the [`mcp`](../deploy/mcp/README.md) capability state -- the only MCP attachment path
4. Loads agent overrides from `<instance>/payloads/agent/payload.yml` `spec.runtime` (merged on top of defaults)
5. Passes everything to the OpenAI Agents SDK `Agent`

## Tracing Configuration

Tracing stores agent request/response data as OpenTelemetry spans in Unity Catalog. It powers the observability dashboard and evaluation pipelines. **Tracing is always on** in App deploys; there is no agent-level opt-in flag.

The destination is wired at App startup by `setup_tracing_early()` (in [payloads/agent/runtime/tracing.py](tracing.py)) using env vars set in [bricks/app/app.yaml](../../../bricks/app/app.yaml):

| Env var | Source | Purpose |
|---------|--------|---------|
| `MLFLOW_EXPERIMENT_ID` | `valueFrom: feedback-experiment` (resolved at deploy time) | Experiment that owns the traces. |
| `TRACING_UC_DESTINATION` | `${var.catalog}.${var.schema_prefix}_agent.mlflow_experiment` | UC OTEL Delta table location. |
| `MLFLOW_TRACING_SQL_WAREHOUSE_ID` | `${var.warehouse_id}` | Warehouse used for UC trace queries (feedback / search-traces). |

Catalog / schema / experiment values are computed by `InstanceConfig` from the target config and the instance's `app_name` -- they are not YAML knobs in `payloads/agent/payload.yml`.

### Disabling tracing

Customers who genuinely need to disable tracing call `mlflow.tracing.disable()` from their own startup hook. The framework no longer ships a config flag because the previous `tracing_enabled` switch did not actually switch anything (traces flowed regardless via the `@invoke()` / `@stream()` decorators).

### GDPR redaction

Set `MLFLOW_TRACE_REDACT_INPUTS=true` and/or `MLFLOW_TRACE_REDACT_OUTPUTS=true` as env vars in your instance's `app/app.yaml` override. The previous `tracing_log_inputs` / `tracing_log_outputs` YAML keys only fired behind the broken `tracing_enabled` gate, so they never redacted anything in production.

## Local Testing

```bash
# Set up auth (CLI profile or env vars)
export DATABRICKS_CONFIG_PROFILE=my-profile

# Point to your instance
export INSTANCE_DIR=instances/<your-app>

# Run the interactive REPL
python -m payloads.agent.runtime.agent
```
