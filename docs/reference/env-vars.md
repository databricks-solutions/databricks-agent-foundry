# Environment variables

<!-- AUTO-GENERATED from agent_foundry/env.py REGISTRY.
     Do not edit by hand; run `python -m agent_foundry.env --generate-docs`. -->

The framework's runtime / deploy / app contracts are expressed via the environment variables listed below.  The single source of truth is ``agent_foundry.env.REGISTRY``; this document is generated from it and kept in sync by a unit test.

## Contexts

- **deploy** -- the ``agent-foundry deploy`` CLI
- **runtime** -- the deployed agent process (Model Serving, Apps backend)
- **app** -- the chat-UI App container (``app.yaml``)
- **eval** -- the local/CI evaluation harness

Variables marked _optional_ have no required context; they tune behaviour but are not part of any boundary contract.

## Registry

| Name | Required at | Default | Description |
| ---- | ----------- | ------- | ----------- |
| `AGENT_FOUNDRY_PROJECT_ROOT` | runtime, app | _(none)_ | Absolute path to the repo root containing instances/ and conf/. The runtime never walks Path(__file__).parents; this variable is the single source of truth for the project root. |
| `INSTANCE_DIR` | runtime, app | _(none)_ | Path (absolute or project-relative) to the active application instance directory containing foundry.toml. |
| `DEPLOY_TARGET` | _optional_ | `dev` | Deployment target name (e.g. 'dev', 'staging', 'prod'). Read by the runtime to load the matching variable file. |
| `AGENT_FOUNDRY_TARGET` | _optional_ | `dev` | Deploy target propagated to subprocesses spawned by the deploy CLI. Equivalent to DEPLOY_TARGET; set internally. |
| `AGENT_MODULE` | app | `payloads.agent.runtime.agent` | Dotted module path to the agent module (must expose `agent`). Defaults to the framework's payloads.agent.runtime.agent. |
| `AGENT_EXTENSIONS` | _optional_ | _(none)_ | Comma-separated dotted module paths of agent payload extensions. Each module exposes `EXTENSION` (see payloads/agent/runtime/extensions.py): extra agent-server routes, chat-proxy paths, per-request tools and a chat-UI overlay directory. Set by a wrapping payload adapter; unset means no extensions. |
| `CHAT_UI_OVERLAYS` | _optional_ | _(none)_ | Comma-separated Python package names whose `ui/overrides/` directory is overlaid onto the chat UI after the agent payload's own overrides, in order (later wins). Set by a wrapping payload adapter; unset means the agent overrides only. |
| `API_PROXY` | app | `http://localhost:8000/invocations` | URL the chat UI proxies invocation requests to. Inside a Databricks App container this is a localhost URL pointing at the AgentServer. |
| `CHAT_APP_PORT` | app | `3000` | Port the chat UI binds to inside the App container. |
| `PORT` | _optional_ | _(none)_ | Fallback port when CHAT_APP_PORT is unset (set by the Databricks Apps platform). |
| `CHAT_PROXY_TIMEOUT_SECONDS` | app | `300` | Upstream invocation timeout for the chat UI proxy. |
| `DATABRICKS_APP_NAME` | _optional_ | _(none)_ | Set by the Databricks Apps platform inside the container. Presence is the canonical 'are we running on Apps?' signal. |
| `MLFLOW_EXPERIMENT_ID` | runtime | _(none)_ | MLflow experiment that the agent logs traces to. Resolved from the App's `feedback-experiment` resource at deploy time. |
| `MLFLOW_TRACING_SQL_WAREHOUSE_ID` | runtime, app | _(none)_ | SQL warehouse used by MLflow's UC tracing destination writer. |
| `TRACING_UC_DESTINATION` | runtime, app | _(none)_ | Three-part Unity Catalog name where MLflow tracing writes spans (catalog.schema.table). |
| `AGENT_SCHEMA` | app | _(none)_ | Two-part Unity Catalog schema (catalog.schema) holding the agent's own objects. Resolved at deploy time because `catalog` lives in the repo-root foundry.toml, which is never staged into the App. |
| `AGENT_CATALOG` | app | _(none)_ | The deploy target's catalog, published so the runtime can resolve `${var.catalog}` in Unity Gateway FQNs (foundry.toml is not read at runtime). |
| `AGENT_SCHEMA_PREFIX` | app | _(none)_ | The instance's schema prefix, published so the runtime can resolve `${var.schema_prefix}` in Unity Gateway FQNs (foundry.toml is not read at runtime). |
| `DATABRICKS_WAREHOUSE_ID` | runtime, app | _(none)_ | Default SQL warehouse for UC function tool calls (sql_function brick) and Genie space queries. |
| `TITLE_MODEL_ENDPOINT` | app | _(none)_ | Model serving endpoint used by the chat UI to generate conversation titles. |
| `MCP_CLIENT_SESSION_TIMEOUT_SECONDS` | _optional_ | _(none)_ | Override for MCP client session read timeout (seconds). Optional. |
| `MCP_TOOLS_CACHE_TTL_SECONDS` | _optional_ | `300` | Freshness (seconds) of the per-caller cache of MCP server tool lists. '0' disables it. A stale list is served for up to an hour while it is refreshed in the background, so upstream tool changes become visible one request after this long. |
| `ENABLE_MCP_SERVER` | app | `false` | Mount the optional MCP server endpoint at /mcp. Truthy values: 'true', '1', 'yes' (case-insensitive). |
| `ENABLE_A2A` | app | `false` | Mount the optional Agent-to-Agent (A2A) endpoint. Truthy values: 'true', '1', 'yes' (case-insensitive). |
| `ENABLE_RESOURCES_SIDEBAR` | app | `false` | Mount the chat sidebar's resources API at /api/agent/resources. Truthy values: 'true', '1', 'yes' (case-insensitive). |
| `ENABLE_FILE_UPLOAD` | app | `false` | Launch the isolated file-extraction service and mount the chat file-upload route. Truthy values: 'true', '1', 'yes' (case-insensitive). Derived by the agent payload adapter from the payloads/agent/payload.yml spec.ui.file_upload toggle. |
| `ENABLE_IMAGE_INPUT` | app | `false` | Accept PNG/JPEG/WebP images in the chat composer and pass them to the model as image content. Truthy values: 'true', '1', 'yes' (case-insensitive). Derived by the agent payload adapter from spec.ui.file_upload.images; only true when spec.ui.file_upload.enabled is also on. |
| `CHAT_HISTORY_SIDEBAR_DAYS` | app | `0` | Chat history sidebar window: GET /api/history lists only chats created in the last N days. '0' lists every chat. Derived by the agent payload adapter from spec.ui.chat_history.sidebar_days. |
| `CHAT_HISTORY_RETENTION_DAYS` | app | `0` | Lakebase chat retention: the chat UI server deletes chats (with messages and votes) whose last message is older than N days. '0' keeps every chat. Derived by the agent payload adapter from spec.ui.chat_history.retention_days. |
| `ENABLE_USER_SKILLS` | app | `false` | Lakebase-backed user skills: the chat UI server stores each user's skills and sends the picked ones with their chat requests. Truthy values: 'true', '1', 'yes' (case-insensitive). Derived by the agent payload adapter from spec.runtime.tools.skills.user_skills (enabled, store: lakebase). |
| `USER_SKILLS_MAX` | app | `20` | How many skills one user may store. Derived by the agent payload adapter from spec.runtime.tools.skills.user_skills.max_skills. |
| `USER_SKILLS_MAX_MD_CHARS` | app | `20000` | Size cap of one user skill's SKILL.md, in characters. Derived by the agent payload adapter from spec.runtime.tools.skills.user_skills.max_skill_md_chars. |
| `ENABLE_COMPACTION` | app | `false` | Conversation compaction: the chat UI server summarizes older turns of a long chat into a Lakebase-persisted summary and sends it in their place. Truthy values: 'true', '1', 'yes' (case-insensitive). Derived by the agent payload adapter from spec.runtime.context.compaction.enabled, which follows spec.ui.chat_history.enabled when unset. |
| `COMPACTION_TRIGGER_TOKENS` | app | `120000` | Estimated prompt tokens at which a chat is compacted after the turn. Derived by the agent payload adapter from spec.runtime.context.compaction.trigger_input_tokens, or below the tool-result clearing threshold when unset. |
| `COMPACTION_KEEP_TURNS` | app | `3` | How many recent user turns compaction keeps verbatim. Derived by the agent payload adapter from spec.runtime.context.compaction.keep_recent_turns. |
| `COMPACTION_INSTRUCTIONS` | app | `` | Instance-specific rules appended to the fixed compaction summary prompt; empty adds none. Derived by the agent payload adapter from spec.runtime.context.compaction.instructions. |
| `COMPACTION_MANUAL_ACTION` | app | `false` | Show the 'Compact conversation' action in the chat header (compaction runs automatically either way). Truthy values: 'true', '1', 'yes' (case-insensitive). Derived by the agent payload adapter from spec.runtime.context.compaction.manual_action. |
| `ENABLE_CHAT_PLAN` | app | `false` | Agent plan: the chat UI server stores each chat's plan in the chat-history Lakebase, serves it to the agent and to the plan panel above the chat input. Truthy values: 'true', '1', 'yes' (case-insensitive). Derived by the agent payload adapter from spec.runtime.context.plan.enabled. |
| `PLAN_MAX_CHARS` | app | `8000` | Longest plan, and longest plan history, the chat UI server stores for one chat, in characters. Derived by the agent payload adapter from spec.runtime.context.plan.max_chars. |
| `ENABLE_AGENT_MEMORY` | app | `false` | Long-term agent memory: the chat UI server stores each user's memory facts in the chat-history Lakebase and serves them to the agent's memory tools. Truthy values: 'true', '1', 'yes' (case-insensitive). Derived by the agent payload adapter from spec.runtime.context.memory.enabled. |
| `AGENT_MEMORY_MAX_TOPIC_CHARS` | app | `8000` | Longest memory topic, its facts one per line, the chat UI server stores for one user, in characters. Derived by the agent payload adapter from spec.runtime.context.memory.max_entry_chars. |
| `ENABLE_SPEECH_INPUT` | app | `false` | Show the chat composer mic button and mount POST /api/transcribe. Truthy values: 'true', '1', 'yes' (case-insensitive). Derived by the agent payload adapter from spec.ui.speech_input.enabled. |
| `SPEECH_BACKEND` | app | `serving_invocations` | Transcription backend of the mic button; 'serving_invocations' posts base64 audio to SPEECH_ENDPOINT. Derived by the agent payload adapter from spec.ui.speech_input.backend. |
| `SPEECH_ENDPOINT` | app | `` | Model Serving endpoint that transcribes the mic recordings. Derived by the agent payload adapter from spec.ui.speech_input.endpoint. |
| `SPEECH_MAX_SECONDS` | app | `300` | Longest recording the mic button records and the transcribe route accepts, in seconds. Derived by the agent payload adapter from spec.ui.speech_input.max_seconds. |
| `SPEECH_LANGUAGE` | app | `` | Optional two-letter language hint for transcription (e.g. 'de'); empty means the browser language. Derived by the agent payload adapter from spec.ui.speech_input.language. |
| `ENABLE_CHARTS` | app | `false` | Render 'echarts-chart' fenced blocks as interactive charts in the chat UI. Truthy values: 'true', '1', 'yes' (case-insensitive). Derived by the agent payload adapter from the payloads/agent/payload.yml spec.ui.charts toggle. |
| `ENABLE_FEEDBACK` | app | `false` | Show the per-message thumbs up/down affordance in the chat UI. Also requires MLFLOW_EXPERIMENT_ID, since feedback is written to the trace. Truthy values: 'true', '1', 'yes' (case-insensitive). Derived by the agent payload adapter from the payloads/agent/payload.yml spec.ui.feedback toggle. |
| `TOOL_OUTPUT_EXPANDED` | app | `false` | Start each tool-call panel in the chat UI expanded instead of collapsed. Truthy values: 'true', '1', 'yes' (case-insensitive). Unset reads as collapsed, which keeps the transcript readable when a turn makes several tool calls; users can still expand any individual panel. Derived by the agent payload adapter from the payloads/agent/payload.yml spec.ui.tool_output.expanded mode. |
| `CHAT_MAX_WIDTH` | app | `4xl` | Maximum width of the chat column in the chat UI, as a Tailwind max-width step: '4xl' (896px), '5xl' (1024px), '6xl' (1152px) or '7xl' (1280px). The column fills the window up to this cap, so a narrow window is unaffected. Unset or an unknown value reads as '4xl'. Derived by the agent payload adapter from the payloads/agent/payload.yml spec.ui.chat_layout.width mode. |
| `FILE_UPLOAD_PORT` | _optional_ | `8001` | Localhost port the isolated file-extraction service binds to inside the App container. |
| `FILE_UPLOAD_MAX_MB` | _optional_ | `10` | Hard upload size cap (megabytes) enforced before parsing in the file-extraction service. Derived by the agent payload adapter from the payloads/agent/payload.yml spec.ui.file_upload.max_mb. |
| `FILE_UPLOAD_MEM_LIMIT_MB` | _optional_ | _(none)_ | Optional address-space ceiling (megabytes) applied to each parse worker in the file-extraction service (POSIX only). Unset = rely on the container memory limit. |
| `EVAL_HTTP_TIMEOUT_SECONDS` | _optional_ | _(none)_ | Override for the evaluation harness's per-request HTTP timeout (seconds). Optional. |
| `DATABRICKS_HOST` | _optional_ | _(none)_ | Databricks workspace URL. Required when using token auth; ignored when DATABRICKS_CONFIG_PROFILE is set. |
| `DATABRICKS_TOKEN` [secret] | _optional_ | _(none)_ | Databricks PAT for non-profile authentication. Required when DATABRICKS_HOST is set without DATABRICKS_CONFIG_PROFILE. |
| `AGENT_FOUNDRY_TELEMETRY_DISABLED` | _optional_ | `false` | When true, omit the agent-foundry SDK User-Agent product token and skip Labs adoption extras. Truthy values: 'true', '1', 'yes' (case-insensitive). |
| `DATABRICKS_CONFIG_PROFILE` | _optional_ | _(none)_ | Databricks CLI profile name. When set, takes precedence over DATABRICKS_HOST/DATABRICKS_TOKEN. |
| `DATABRICKS_CLIENT_ID` | _optional_ | _(none)_ | OAuth M2M service-principal client id. Used with DATABRICKS_CLIENT_SECRET. |
| `DATABRICKS_CLIENT_SECRET` [secret] | _optional_ | _(none)_ | OAuth M2M service-principal secret. Used with DATABRICKS_CLIENT_ID. |
| `DATABRICKS_AUTH_TYPE` | _optional_ | _(none)_ | Databricks SDK auth method override. 'github-oidc' with DATABRICKS_CLIENT_ID exchanges the GitHub Actions OIDC token through a service principal federation policy (no stored secret). |
| `AGENT_FOUNDRY_TEST_CATALOG` | _optional_ | _(none)_ | Unity Catalog name written into generated foundry.toml for acceptance deploys. Never embed a workspace URL here; host comes from DATABRICKS_HOST. |
| `AGENT_FOUNDRY_TEST_VS_ENDPOINT` | _optional_ | _(none)_ | Existing Vector Search endpoint the brick canary builds its index on. Kept between runs; destroy removes only the index. |
| `AGENT_FOUNDRY_TEST_MODEL_SERVICE` | _optional_ | _(none)_ | Unity Gateway model service FQN (catalog.schema.name) for the serving live checks' model-service variants. Unset skips them. |
| `AGENT_FOUNDRY_TEST_GATEWAY_SKILL` | _optional_ | _(none)_ | Unity Catalog skill FQN (catalog.schema.name) the gateway-skills live checks read as the test principal. Unset skips them. |
| `TEST_WAREHOUSE_ID` | _optional_ | _(none)_ | Existing SQL warehouse id for live tests. agent-foundry test/deploy --run-tests export it from the target's warehouse_id. |
| `AGENT_FOUNDRY_ACCEPTANCE_SCENARIOS` | _optional_ | _(none)_ | Comma-separated scenario ids from agent_foundry.testing.scenarios.toml to run. Empty means the full catalog. |
| `AGENT_FOUNDRY_RUN_ID` | _optional_ | _(none)_ | Opaque run token used to mint unique App/schema names for ephemeral upstream acceptance. GitHub Actions may omit this and use GITHUB_RUN_ID. |
| `AGENT_FOUNDRY_REQUIRE_LIVE` | _optional_ | _(none)_ | Set to 1 in canary jobs: a tests/live check whose workspace binding or deployed instance is missing fails instead of skipping. |

## Reading values in code

```python
from agent_foundry import env

# Required (raises MissingEnvVar when no value and no default):
root = env.read(env.AGENT_FOUNDRY_PROJECT_ROOT)

# Optional:
profile = env.read_optional(env.DATABRICKS_CONFIG_PROFILE)

# Typed parsers:
port = env.read_int(env.CHAT_APP_PORT, fallback=3000)
enabled = env.read_bool(env.ENABLE_MCP_SERVER)

# Fail-fast at a context boundary:
env.validate_for("runtime")  # raises listing every missing var
```
