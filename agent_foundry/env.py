"""
Central env-var registry (issue #155).

Every environment variable that participates in the framework's runtime
or deploy contract is declared here, in one place, with:

* A typed ``EnvVar`` descriptor (name, contexts where it is required,
  description, optional default).
* Helper functions for reading values (``read``, ``read_optional``,
  ``read_int``, ``read_bool``).
* A per-context fail-fast validator (``validate_for("runtime" |
  "deploy" | "app" | "eval")``).

Why a registry?

Before this module existed, env-var contracts were spread across three
surfaces (``bricks/agent/core.py``, ``bricks/app/scripts/start_app.py``,
``bricks/*/app.yaml``) with no shared source.  Names drifted, defaults
diverged, and adding a new variable required hand-editing every
template.  Issue #155 consolidates the contract here so that:

* ``app.yaml`` templates (#158) can be generated from this registry.
* Each entry point (the deploy CLI, the runtime, the chat App) can call
  :func:`validate_for` at its boundary and fail fast with a clear message.
* Documentation (``docs/reference/env-vars.md``) is generated from this module and
  kept in sync by a unit test.

Validation policy
-----------------

This module deliberately does **not** validate at import time.  Each
context (deploy, runtime, app, eval) sets a different subset of the
variables; an import-time check would break tests, dry-runs, and the
deploy CLI itself.  Callers that need fail-fast behaviour invoke
:func:`validate_for` at the relevant boundary.
"""

from __future__ import annotations

import os
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict

Context = Literal["runtime", "deploy", "app", "eval"]


class MissingEnvVar(RuntimeError):
    """Raised when a required env var is unset and has no default."""


class EnvVar(BaseModel):
    """Typed descriptor for one framework environment variable."""

    model_config = ConfigDict(frozen=True)

    name: str
    required_at: tuple[Context, ...]
    description: str
    default: str | None = None
    sensitive: bool = False


# ---------------------------------------------------------------------------
# Registry entries
# ---------------------------------------------------------------------------
#
# Grouped by topic for readability.  Order within a group is not load-bearing,
# but the public ``REGISTRY`` tuple at the bottom is the single source of
# truth for every consumer (template generation, docs, validation).

# --- Project / instance contract (runtime + app) ---------------------------

AGENT_FOUNDRY_PROJECT_ROOT: Final = EnvVar(
    name="AGENT_FOUNDRY_PROJECT_ROOT",
    required_at=("runtime", "app"),
    description=(
        "Absolute path to the repo root containing instances/ and conf/. "
        "The runtime never walks Path(__file__).parents; this variable is "
        "the single source of truth for the project root."
    ),
)

INSTANCE_DIR: Final = EnvVar(
    name="INSTANCE_DIR",
    required_at=("runtime", "app"),
    description=(
        "Path (absolute or project-relative) to the active application instance directory containing foundry.toml."
    ),
)

DEPLOY_TARGET: Final = EnvVar(
    name="DEPLOY_TARGET",
    required_at=(),
    description=(
        "Deployment target name (e.g. 'dev', 'staging', 'prod'). Read by "
        "the runtime to load the matching variable file."
    ),
    default="dev",
)

AGENT_FOUNDRY_TARGET: Final = EnvVar(
    name="AGENT_FOUNDRY_TARGET",
    required_at=(),
    description=(
        "Deploy target propagated to subprocesses spawned by the deploy "
        "CLI. Equivalent to DEPLOY_TARGET; set internally."
    ),
    default="dev",
)

# --- Agent app boot contract -----------------------------------------------

AGENT_MODULE: Final = EnvVar(
    name="AGENT_MODULE",
    required_at=("app",),
    description=(
        "Dotted module path to the agent module (must expose `agent`). "
        "Defaults to the framework's payloads.agent.runtime.agent."
    ),
    default="payloads.agent.runtime.agent",
)

AGENT_EXTENSIONS: Final = EnvVar(
    name="AGENT_EXTENSIONS",
    required_at=(),
    description=(
        "Comma-separated dotted module paths of agent payload extensions. Each module "
        "exposes `EXTENSION` (see payloads/agent/runtime/extensions.py): extra agent-server "
        "routes, chat-proxy paths, per-request tools and a chat-UI overlay directory. "
        "Set by a wrapping payload adapter; unset means no extensions."
    ),
)

CHAT_UI_OVERLAYS: Final = EnvVar(
    name="CHAT_UI_OVERLAYS",
    required_at=(),
    description=(
        "Comma-separated Python package names whose `ui/overrides/` directory is overlaid "
        "onto the chat UI after the agent payload's own overrides, in order (later wins). "
        "Set by a wrapping payload adapter; unset means the agent overrides only."
    ),
)

API_PROXY: Final = EnvVar(
    name="API_PROXY",
    required_at=("app",),
    description=(
        "URL the chat UI proxies invocation requests to. Inside a "
        "Databricks App container this is a localhost URL pointing at "
        "the AgentServer."
    ),
    default="http://localhost:8000/invocations",
)

CHAT_APP_PORT: Final = EnvVar(
    name="CHAT_APP_PORT",
    required_at=("app",),
    description="Port the chat UI binds to inside the App container.",
    default="3000",
)

PORT: Final = EnvVar(
    name="PORT",
    required_at=(),
    description=("Fallback port when CHAT_APP_PORT is unset (set by the Databricks Apps platform)."),
)

CHAT_PROXY_TIMEOUT_SECONDS: Final = EnvVar(
    name="CHAT_PROXY_TIMEOUT_SECONDS",
    required_at=("app",),
    description="Upstream invocation timeout for the chat UI proxy.",
    default="300",
)

DATABRICKS_APP_NAME: Final = EnvVar(
    name="DATABRICKS_APP_NAME",
    required_at=(),
    description=(
        "Set by the Databricks Apps platform inside the container. "
        "Presence is the canonical 'are we running on Apps?' signal."
    ),
)

# --- MLflow / tracing contract ---------------------------------------------

MLFLOW_EXPERIMENT_ID: Final = EnvVar(
    name="MLFLOW_EXPERIMENT_ID",
    required_at=("runtime",),
    description=(
        "MLflow experiment that the agent logs traces to. Resolved from "
        "the App's `feedback-experiment` resource at deploy time."
    ),
)

MLFLOW_TRACING_SQL_WAREHOUSE_ID: Final = EnvVar(
    name="MLFLOW_TRACING_SQL_WAREHOUSE_ID",
    required_at=("runtime", "app"),
    description=("SQL warehouse used by MLflow's UC tracing destination writer."),
)

TRACING_UC_DESTINATION: Final = EnvVar(
    name="TRACING_UC_DESTINATION",
    required_at=("runtime", "app"),
    description=("Three-part Unity Catalog name where MLflow tracing writes spans (catalog.schema.table)."),
)

AGENT_SCHEMA: Final = EnvVar(
    name="AGENT_SCHEMA",
    required_at=("app",),
    description=(
        "Two-part Unity Catalog schema (catalog.schema) holding the agent's own "
        "objects. Resolved at deploy time because `catalog` lives in the repo-root "
        "foundry.toml, which is never staged into the App."
    ),
)

AGENT_CATALOG: Final = EnvVar(
    name="AGENT_CATALOG",
    required_at=("app",),
    description=(
        "The deploy target's catalog, published so the runtime can resolve "
        "`${var.catalog}` in Unity Gateway FQNs (foundry.toml is not read at runtime)."
    ),
)

AGENT_SCHEMA_PREFIX: Final = EnvVar(
    name="AGENT_SCHEMA_PREFIX",
    required_at=("app",),
    description=(
        "The instance's schema prefix, published so the runtime can resolve "
        "`${var.schema_prefix}` in Unity Gateway FQNs (foundry.toml is not read at runtime)."
    ),
)

# --- Tool / model endpoint contract ----------------------------------------

DATABRICKS_WAREHOUSE_ID: Final = EnvVar(
    name="DATABRICKS_WAREHOUSE_ID",
    required_at=("runtime", "app"),
    description=("Default SQL warehouse for UC function tool calls (sql_function brick) and Genie space queries."),
)

TITLE_MODEL_ENDPOINT: Final = EnvVar(
    name="TITLE_MODEL_ENDPOINT",
    required_at=("app",),
    description=("Model serving endpoint used by the chat UI to generate conversation titles."),
)

MCP_CLIENT_SESSION_TIMEOUT_SECONDS: Final = EnvVar(
    name="MCP_CLIENT_SESSION_TIMEOUT_SECONDS",
    required_at=(),
    description=("Override for MCP client session read timeout (seconds). Optional."),
)

MCP_TOOLS_CACHE_TTL_SECONDS: Final = EnvVar(
    name="MCP_TOOLS_CACHE_TTL_SECONDS",
    required_at=(),
    default="300",
    description=(
        "Freshness (seconds) of the per-caller cache of MCP server tool lists. '0' disables it. "
        "A stale list is served for up to an hour while it is refreshed in the background, "
        "so upstream tool changes become visible one request after this long."
    ),
)

# --- Feature toggles (app) -------------------------------------------------

ENABLE_MCP_SERVER: Final = EnvVar(
    name="ENABLE_MCP_SERVER",
    required_at=("app",),
    description=(
        "Mount the optional MCP server endpoint at /mcp. Truthy values: 'true', '1', 'yes' (case-insensitive)."
    ),
    default="false",
)

ENABLE_A2A: Final = EnvVar(
    name="ENABLE_A2A",
    required_at=("app",),
    description=(
        "Mount the optional Agent-to-Agent (A2A) endpoint. Truthy values: 'true', '1', 'yes' (case-insensitive)."
    ),
    default="false",
)

ENABLE_RESOURCES_SIDEBAR: Final = EnvVar(
    name="ENABLE_RESOURCES_SIDEBAR",
    required_at=("app",),
    description=(
        "Mount the chat sidebar's resources API at /api/agent/resources. "
        "Truthy values: 'true', '1', 'yes' (case-insensitive)."
    ),
    default="false",
)

ENABLE_FILE_UPLOAD: Final = EnvVar(
    name="ENABLE_FILE_UPLOAD",
    required_at=("app",),
    description=(
        "Launch the isolated file-extraction service and mount the chat file-upload route. "
        "Truthy values: 'true', '1', 'yes' (case-insensitive). Derived by the agent payload "
        "adapter from the payloads/agent/payload.yml spec.ui.file_upload toggle."
    ),
    default="false",
)

ENABLE_IMAGE_INPUT: Final = EnvVar(
    name="ENABLE_IMAGE_INPUT",
    required_at=("app",),
    description=(
        "Accept PNG/JPEG/WebP images in the chat composer and pass them to the model as "
        "image content. Truthy values: 'true', '1', 'yes' (case-insensitive). Derived by "
        "the agent payload adapter from spec.ui.file_upload.images; only true when "
        "spec.ui.file_upload.enabled is also on."
    ),
    default="false",
)

CHAT_HISTORY_SIDEBAR_DAYS: Final = EnvVar(
    name="CHAT_HISTORY_SIDEBAR_DAYS",
    required_at=("app",),
    description=(
        "Chat history sidebar window: GET /api/history lists only chats created in the last "
        "N days. '0' lists every chat. Derived by the agent payload adapter from "
        "spec.ui.chat_history.sidebar_days."
    ),
    default="0",
)

CHAT_HISTORY_RETENTION_DAYS: Final = EnvVar(
    name="CHAT_HISTORY_RETENTION_DAYS",
    required_at=("app",),
    description=(
        "Lakebase chat retention: the chat UI server deletes chats (with messages and votes) "
        "whose last message is older than N days. '0' keeps every chat. Derived by the agent "
        "payload adapter from spec.ui.chat_history.retention_days."
    ),
    default="0",
)

ENABLE_USER_SKILLS: Final = EnvVar(
    name="ENABLE_USER_SKILLS",
    required_at=("app",),
    description=(
        "Lakebase-backed user skills: the chat UI server stores each user's skills and sends the "
        "picked ones with their chat requests. Truthy values: 'true', '1', 'yes' "
        "(case-insensitive). Derived by the agent payload adapter from "
        "spec.runtime.tools.skills.user_skills (enabled, store: lakebase)."
    ),
    default="false",
)

USER_SKILLS_MAX: Final = EnvVar(
    name="USER_SKILLS_MAX",
    required_at=("app",),
    description=(
        "How many skills one user may store. Derived by the agent payload adapter from "
        "spec.runtime.tools.skills.user_skills.max_skills."
    ),
    default="20",
)

USER_SKILLS_MAX_MD_CHARS: Final = EnvVar(
    name="USER_SKILLS_MAX_MD_CHARS",
    required_at=("app",),
    description=(
        "Size cap of one user skill's SKILL.md, in characters. Derived by the agent payload "
        "adapter from spec.runtime.tools.skills.user_skills.max_skill_md_chars."
    ),
    default="20000",
)

ENABLE_COMPACTION: Final = EnvVar(
    name="ENABLE_COMPACTION",
    required_at=("app",),
    description=(
        "Conversation compaction: the chat UI server summarizes older turns of a long chat into a "
        "Lakebase-persisted summary and sends it in their place. Truthy values: 'true', '1', 'yes' "
        "(case-insensitive). Derived by the agent payload adapter from "
        "spec.runtime.context.compaction.enabled, which follows spec.ui.chat_history.enabled when unset."
    ),
    default="false",
)

COMPACTION_TRIGGER_TOKENS: Final = EnvVar(
    name="COMPACTION_TRIGGER_TOKENS",
    required_at=("app",),
    description=(
        "Estimated prompt tokens at which a chat is compacted after the turn. Derived by the agent "
        "payload adapter from spec.runtime.context.compaction.trigger_input_tokens, or below the "
        "tool-result clearing threshold when unset."
    ),
    default="120000",
)

COMPACTION_KEEP_TURNS: Final = EnvVar(
    name="COMPACTION_KEEP_TURNS",
    required_at=("app",),
    description=(
        "How many recent user turns compaction keeps verbatim. Derived by the agent payload adapter "
        "from spec.runtime.context.compaction.keep_recent_turns."
    ),
    default="3",
)

COMPACTION_INSTRUCTIONS: Final = EnvVar(
    name="COMPACTION_INSTRUCTIONS",
    required_at=("app",),
    description=(
        "Instance-specific rules appended to the fixed compaction summary prompt; empty adds none. "
        "Derived by the agent payload adapter from spec.runtime.context.compaction.instructions."
    ),
    default="",
)

ENABLE_CHAT_PLAN: Final = EnvVar(
    name="ENABLE_CHAT_PLAN",
    required_at=("app",),
    description=(
        "Agent plan: the chat UI server stores each chat's plan in the chat-history Lakebase, "
        "serves it to the agent and to the plan panel above the chat input. "
        "Truthy values: 'true', '1', 'yes' (case-insensitive). "
        "Derived by the agent payload adapter from spec.runtime.context.plan.enabled."
    ),
    default="false",
)

PLAN_MAX_CHARS: Final = EnvVar(
    name="PLAN_MAX_CHARS",
    required_at=("app",),
    description=(
        "Longest plan, and longest plan history, the chat UI server stores for one chat, in "
        "characters. Derived by the agent payload adapter from spec.runtime.context.plan.max_chars."
    ),
    default="8000",
)

ENABLE_AGENT_MEMORY: Final = EnvVar(
    name="ENABLE_AGENT_MEMORY",
    required_at=("app",),
    description=(
        "Long-term agent memory: the chat UI server stores each user's memory facts in the "
        "chat-history Lakebase and serves them to the agent's memory tools. "
        "Truthy values: 'true', '1', 'yes' (case-insensitive). "
        "Derived by the agent payload adapter from spec.runtime.context.memory.enabled."
    ),
    default="false",
)

AGENT_MEMORY_MAX_TOPIC_CHARS: Final = EnvVar(
    name="AGENT_MEMORY_MAX_TOPIC_CHARS",
    required_at=("app",),
    description=(
        "Longest memory topic, its facts one per line, the chat UI server stores for one user, in "
        "characters. Derived by the agent payload adapter from spec.runtime.context.memory.max_entry_chars."
    ),
    default="8000",
)

ENABLE_SPEECH_INPUT: Final = EnvVar(
    name="ENABLE_SPEECH_INPUT",
    required_at=("app",),
    description=(
        "Show the chat composer mic button and mount POST /api/transcribe. "
        "Truthy values: 'true', '1', 'yes' (case-insensitive). "
        "Derived by the agent payload adapter from spec.ui.speech_input.enabled."
    ),
    default="false",
)

SPEECH_BACKEND: Final = EnvVar(
    name="SPEECH_BACKEND",
    required_at=("app",),
    description=(
        "Transcription backend of the mic button; 'serving_invocations' posts base64 audio to "
        "SPEECH_ENDPOINT. Derived by the agent payload adapter from spec.ui.speech_input.backend."
    ),
    default="serving_invocations",
)

SPEECH_ENDPOINT: Final = EnvVar(
    name="SPEECH_ENDPOINT",
    required_at=("app",),
    description=(
        "Model Serving endpoint that transcribes the mic recordings. "
        "Derived by the agent payload adapter from spec.ui.speech_input.endpoint."
    ),
    default="",
)

SPEECH_MAX_SECONDS: Final = EnvVar(
    name="SPEECH_MAX_SECONDS",
    required_at=("app",),
    description=(
        "Longest recording the mic button records and the transcribe route accepts, in seconds. "
        "Derived by the agent payload adapter from spec.ui.speech_input.max_seconds."
    ),
    default="300",
)

SPEECH_LANGUAGE: Final = EnvVar(
    name="SPEECH_LANGUAGE",
    required_at=("app",),
    description=(
        "Optional two-letter language hint for transcription (e.g. 'de'); empty means the browser "
        "language. Derived by the agent payload adapter from spec.ui.speech_input.language."
    ),
    default="",
)

COMPACTION_MANUAL_ACTION: Final = EnvVar(
    name="COMPACTION_MANUAL_ACTION",
    required_at=("app",),
    description=(
        "Show the 'Compact conversation' action in the chat header (compaction runs automatically "
        "either way). Truthy values: 'true', '1', 'yes' (case-insensitive). Derived by the agent "
        "payload adapter from spec.runtime.context.compaction.manual_action."
    ),
    default="false",
)

# --- Chat UI presentation toggles ------------------------------------------
#
# Read only by the chat UI's Node config route
# (payloads/agent/ui/overrides/server/src/routes/config.ts), which serves
# them to the client as ``/api/config`` feature flags.  No Python
# consumer except ENABLE_CHARTS and ENABLE_REPORTS (the agent's ``_chart``
# and ``_report`` tools), but registered here so the App boundary contract
# stays in one place and reaches docs/reference/env-vars.md.

ENABLE_CHARTS: Final = EnvVar(
    name="ENABLE_CHARTS",
    required_at=("app",),
    description=(
        "Render 'echarts-chart' fenced blocks as interactive charts in the chat UI, and offer the "
        "agent the '_chart' tool, which charts a SQL query's result. Truthy values: 'true', '1', "
        "'yes' (case-insensitive). Derived by the agent payload adapter from the "
        "payloads/agent/payload.yml spec.ui.charts toggle."
    ),
    default="false",
)

ENABLE_REPORTS: Final = EnvVar(
    name="ENABLE_REPORTS",
    required_at=("app",),
    description=(
        "Render 'markdown-report' fenced blocks as downloadable Markdown reports in the chat UI, and "
        "offer the agent the '_report' tool. Truthy values: 'true', '1', 'yes' (case-insensitive). "
        "Derived by the agent payload adapter from the payloads/agent/payload.yml spec.ui.reports toggle."
    ),
    default="false",
)

ENABLE_FEEDBACK: Final = EnvVar(
    name="ENABLE_FEEDBACK",
    required_at=("app",),
    description=(
        "Show the per-message thumbs up/down affordance in the chat UI. Also requires "
        "MLFLOW_EXPERIMENT_ID, since feedback is written to the trace. Truthy values: "
        "'true', '1', 'yes' (case-insensitive). Derived by the agent payload adapter from "
        "the payloads/agent/payload.yml spec.ui.feedback toggle."
    ),
    default="false",
)

TOOL_OUTPUT_EXPANDED: Final = EnvVar(
    name="TOOL_OUTPUT_EXPANDED",
    required_at=("app",),
    description=(
        "Start each tool-call panel in the chat UI expanded instead of collapsed. "
        "Truthy values: 'true', '1', 'yes' (case-insensitive). Unset reads as collapsed, "
        "which keeps the transcript readable when a turn makes several tool calls; users "
        "can still expand any individual panel. Derived by the agent payload adapter from "
        "the payloads/agent/payload.yml spec.ui.tool_output.expanded mode."
    ),
    default="false",
)

CHAT_MAX_WIDTH: Final = EnvVar(
    name="CHAT_MAX_WIDTH",
    required_at=("app",),
    description=(
        "Maximum width of the chat column in the chat UI, as a Tailwind max-width step: "
        "'4xl' (896px), '5xl' (1024px), '6xl' (1152px) or '7xl' (1280px). The column fills "
        "the window up to this cap, so a narrow window is unaffected. Unset or an unknown "
        "value reads as '4xl'. Derived by the agent payload adapter from the "
        "payloads/agent/payload.yml spec.ui.chat_layout.width mode."
    ),
    default="4xl",
)

# --- File upload / extractor service ---------------------------------------

FILE_UPLOAD_PORT: Final = EnvVar(
    name="FILE_UPLOAD_PORT",
    required_at=(),
    description="Localhost port the isolated file-extraction service binds to inside the App container.",
    default="8001",
)

FILE_UPLOAD_MAX_MB: Final = EnvVar(
    name="FILE_UPLOAD_MAX_MB",
    required_at=(),
    description=(
        "Hard upload size cap (megabytes) enforced before parsing in the file-extraction service. "
        "Derived by the agent payload adapter from the payloads/agent/payload.yml "
        "spec.ui.file_upload.max_mb."
    ),
    default="10",
)

FILE_UPLOAD_MEM_LIMIT_MB: Final = EnvVar(
    name="FILE_UPLOAD_MEM_LIMIT_MB",
    required_at=(),
    description=(
        "Optional address-space ceiling (megabytes) applied to each parse worker in the "
        "file-extraction service (POSIX only). Unset = rely on the container memory limit."
    ),
)

FILE_UPLOAD_MAX_CONCURRENCY: Final = EnvVar(
    name="FILE_UPLOAD_MAX_CONCURRENCY",
    required_at=(),
    description=(
        "Maximum number of uploads parsed simultaneously by the file-extraction service. "
        "Caps concurrent parse workers so a single authenticated user flooding /extract "
        "cannot exhaust the container (CPU/memory); excess requests get HTTP 429."
    ),
    default="4",
)

# --- Evaluation harness ----------------------------------------------------

EVAL_HTTP_TIMEOUT_SECONDS: Final = EnvVar(
    name="EVAL_HTTP_TIMEOUT_SECONDS",
    required_at=(),
    description=("Override for the evaluation harness's per-request HTTP timeout (seconds). Optional."),
)

# --- Databricks SDK credentials --------------------------------------------
#
# These are documented for completeness but are owned by the Databricks
# SDK auth chain, not the framework.  Listed here so that deploy-time
# validation can catch the common 'no auth configured' case before
# attempting the first API call.

DATABRICKS_HOST: Final = EnvVar(
    name="DATABRICKS_HOST",
    required_at=(),
    description=(
        "Databricks workspace URL. Required when using token auth; ignored when DATABRICKS_CONFIG_PROFILE is set."
    ),
)

DATABRICKS_TOKEN: Final = EnvVar(
    name="DATABRICKS_TOKEN",
    required_at=(),
    description=(
        "Databricks PAT for non-profile authentication. Required when "
        "DATABRICKS_HOST is set without DATABRICKS_CONFIG_PROFILE."
    ),
    sensitive=True,
)

AGENT_FOUNDRY_TELEMETRY_DISABLED: Final = EnvVar(
    name="AGENT_FOUNDRY_TELEMETRY_DISABLED",
    required_at=(),
    description=(
        "When true, omit the agent-foundry SDK User-Agent product token and skip "
        "Labs adoption extras. Truthy values: 'true', '1', 'yes' (case-insensitive)."
    ),
    default="false",
)

DATABRICKS_CONFIG_PROFILE: Final = EnvVar(
    name="DATABRICKS_CONFIG_PROFILE",
    required_at=(),
    description=("Databricks CLI profile name. When set, takes precedence over DATABRICKS_HOST/DATABRICKS_TOKEN."),
)

DATABRICKS_CLIENT_ID: Final = EnvVar(
    name="DATABRICKS_CLIENT_ID",
    required_at=(),
    description="OAuth M2M service-principal client id. Used with DATABRICKS_CLIENT_SECRET.",
)

DATABRICKS_CLIENT_SECRET: Final = EnvVar(
    name="DATABRICKS_CLIENT_SECRET",
    required_at=(),
    description="OAuth M2M service-principal secret. Used with DATABRICKS_CLIENT_ID.",
    sensitive=True,
)

DATABRICKS_AUTH_TYPE: Final = EnvVar(
    name="DATABRICKS_AUTH_TYPE",
    required_at=(),
    description=(
        "Databricks SDK auth method override. 'github-oidc' with DATABRICKS_CLIENT_ID exchanges "
        "the GitHub Actions OIDC token through a service principal federation policy (no stored secret)."
    ),
)

AGENT_FOUNDRY_TEST_CATALOG: Final = EnvVar(
    name="AGENT_FOUNDRY_TEST_CATALOG",
    required_at=(),
    description=(
        "Unity Catalog name written into generated foundry.toml for acceptance "
        "deploys. Never embed a workspace URL here; host comes from DATABRICKS_HOST."
    ),
)

AGENT_FOUNDRY_TEST_VS_ENDPOINT: Final = EnvVar(
    name="AGENT_FOUNDRY_TEST_VS_ENDPOINT",
    required_at=(),
    description=(
        "Existing Vector Search endpoint the brick canary builds its index on. "
        "Kept between runs; destroy removes only the index."
    ),
)

AGENT_FOUNDRY_TEST_MODEL_SERVICE: Final = EnvVar(
    name="AGENT_FOUNDRY_TEST_MODEL_SERVICE",
    required_at=(),
    description=(
        "Unity Gateway model service FQN (catalog.schema.name) for the serving live checks' "
        "model-service variants. Unset skips them."
    ),
)

AGENT_FOUNDRY_TEST_GATEWAY_SKILL: Final = EnvVar(
    name="AGENT_FOUNDRY_TEST_GATEWAY_SKILL",
    required_at=(),
    description=(
        "Unity Catalog skill FQN (catalog.schema.name) the gateway-skills live checks read "
        "as the test principal. Unset skips them."
    ),
)

TEST_WAREHOUSE_ID: Final = EnvVar(
    name="TEST_WAREHOUSE_ID",
    required_at=(),
    description=(
        "Existing SQL warehouse id for live tests. "
        "agent-foundry test/deploy --run-tests export it from the target's warehouse_id."
    ),
)

AGENT_FOUNDRY_ACCEPTANCE_SCENARIOS: Final = EnvVar(
    name="AGENT_FOUNDRY_ACCEPTANCE_SCENARIOS",
    required_at=(),
    description=(
        "Comma-separated scenario ids from agent_foundry.testing.scenarios.toml to run. Empty means the full catalog."
    ),
)

AGENT_FOUNDRY_RUN_ID: Final = EnvVar(
    name="AGENT_FOUNDRY_RUN_ID",
    required_at=(),
    description=(
        "Opaque run token used to mint unique App/schema names for ephemeral "
        "upstream acceptance. GitHub Actions may omit this and use GITHUB_RUN_ID."
    ),
)

AGENT_FOUNDRY_REQUIRE_LIVE: Final = EnvVar(
    name="AGENT_FOUNDRY_REQUIRE_LIVE",
    required_at=(),
    description=(
        "Set to 1 in canary jobs: a tests/live check whose workspace binding or deployed "
        "instance is missing fails instead of skipping."
    ),
)


# ---------------------------------------------------------------------------
# Public registry tuple
# ---------------------------------------------------------------------------

REGISTRY: Final[tuple[EnvVar, ...]] = (
    AGENT_FOUNDRY_PROJECT_ROOT,
    INSTANCE_DIR,
    DEPLOY_TARGET,
    AGENT_FOUNDRY_TARGET,
    AGENT_MODULE,
    AGENT_EXTENSIONS,
    CHAT_UI_OVERLAYS,
    API_PROXY,
    CHAT_APP_PORT,
    PORT,
    CHAT_PROXY_TIMEOUT_SECONDS,
    DATABRICKS_APP_NAME,
    MLFLOW_EXPERIMENT_ID,
    MLFLOW_TRACING_SQL_WAREHOUSE_ID,
    TRACING_UC_DESTINATION,
    AGENT_SCHEMA,
    AGENT_CATALOG,
    AGENT_SCHEMA_PREFIX,
    DATABRICKS_WAREHOUSE_ID,
    TITLE_MODEL_ENDPOINT,
    MCP_CLIENT_SESSION_TIMEOUT_SECONDS,
    MCP_TOOLS_CACHE_TTL_SECONDS,
    ENABLE_MCP_SERVER,
    ENABLE_A2A,
    ENABLE_RESOURCES_SIDEBAR,
    ENABLE_FILE_UPLOAD,
    ENABLE_IMAGE_INPUT,
    CHAT_HISTORY_SIDEBAR_DAYS,
    CHAT_HISTORY_RETENTION_DAYS,
    ENABLE_USER_SKILLS,
    USER_SKILLS_MAX,
    USER_SKILLS_MAX_MD_CHARS,
    ENABLE_COMPACTION,
    COMPACTION_TRIGGER_TOKENS,
    COMPACTION_KEEP_TURNS,
    COMPACTION_INSTRUCTIONS,
    COMPACTION_MANUAL_ACTION,
    ENABLE_CHAT_PLAN,
    PLAN_MAX_CHARS,
    ENABLE_AGENT_MEMORY,
    AGENT_MEMORY_MAX_TOPIC_CHARS,
    ENABLE_SPEECH_INPUT,
    SPEECH_BACKEND,
    SPEECH_ENDPOINT,
    SPEECH_MAX_SECONDS,
    SPEECH_LANGUAGE,
    ENABLE_CHARTS,
    ENABLE_REPORTS,
    ENABLE_FEEDBACK,
    TOOL_OUTPUT_EXPANDED,
    CHAT_MAX_WIDTH,
    FILE_UPLOAD_PORT,
    FILE_UPLOAD_MAX_MB,
    FILE_UPLOAD_MEM_LIMIT_MB,
    FILE_UPLOAD_MAX_CONCURRENCY,
    EVAL_HTTP_TIMEOUT_SECONDS,
    DATABRICKS_HOST,
    DATABRICKS_TOKEN,
    AGENT_FOUNDRY_TELEMETRY_DISABLED,
    DATABRICKS_CONFIG_PROFILE,
    DATABRICKS_CLIENT_ID,
    DATABRICKS_CLIENT_SECRET,
    DATABRICKS_AUTH_TYPE,
    AGENT_FOUNDRY_TEST_CATALOG,
    AGENT_FOUNDRY_TEST_VS_ENDPOINT,
    AGENT_FOUNDRY_TEST_MODEL_SERVICE,
    AGENT_FOUNDRY_TEST_GATEWAY_SKILL,
    TEST_WAREHOUSE_ID,
    AGENT_FOUNDRY_ACCEPTANCE_SCENARIOS,
    AGENT_FOUNDRY_RUN_ID,
    AGENT_FOUNDRY_REQUIRE_LIVE,
)


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------


def read(var: EnvVar) -> str:
    """Return the value of *var* from the environment, falling back to its
    declared default.  Raises :class:`MissingEnvVar` when neither is set.
    """
    value = os.environ.get(var.name)
    if value is not None and value != "":
        return value
    if var.default is not None:
        return var.default
    raise MissingEnvVar(
        f"Required environment variable {var.name!r} is not set (no default declared). {var.description}"
    )


def read_optional(var: EnvVar) -> str | None:
    """Return the value of *var* or ``None`` when neither value nor
    default are set.  Never raises.
    """
    value = os.environ.get(var.name)
    if value is not None and value != "":
        return value
    return var.default


def read_int(var: EnvVar, *, fallback: int | None = None) -> int | None:
    """Read *var* and parse as int.  Returns *fallback* (default
    ``None``) when the value is unset, empty, or cannot be parsed.
    """
    raw = read_optional(var)
    if raw is None or raw == "":
        return fallback
    try:
        return int(raw)
    except ValueError:
        return fallback


def read_bool(var: EnvVar, *, fallback: bool = False) -> bool:
    """Read *var* and parse as bool ('true'/'1'/'yes' truthy,
    case-insensitive).  Returns *fallback* when unset.
    """
    raw = read_optional(var)
    if raw is None:
        return fallback
    return raw.strip().lower() in {"true", "1", "yes"}


def on_databricks_apps() -> bool:
    """True inside a Databricks App container (``DATABRICKS_APP_NAME`` is set)."""
    return read_optional(DATABRICKS_APP_NAME) is not None


# ---------------------------------------------------------------------------
# Per-context fail-fast validation
# ---------------------------------------------------------------------------


def vars_for(context: Context) -> tuple[EnvVar, ...]:
    """Return every registered :class:`EnvVar` that lists *context* in
    ``required_at``.  Used by template generators (#158) and the docs
    sync test.
    """
    return tuple(v for v in REGISTRY if context in v.required_at)


def validate_for(context: Context) -> None:
    """Verify every variable required at *context* has a value (or
    default).  Raises :class:`MissingEnvVar` aggregating all missing
    names so callers see one clear error instead of repeated import-time
    failures.

    Intended call sites:

    * ``runtime`` -- ``payloads.agent.runtime.core`` at module init.
    * ``app``     -- ``bricks.app.scripts.start_app`` at boot.
    * ``deploy``  -- ``agent_foundry.deploy.cli`` after argv parsing.
    * ``eval``    -- ``bricks.evaluation.scripts.run_evaluation`` entry.
    """
    missing: list[str] = []
    for var in vars_for(context):
        if read_optional(var) is None:
            missing.append(var.name)
    if missing:
        raise MissingEnvVar(
            f"Missing required environment variable(s) for {context!r}: "
            f"{', '.join(missing)}. See docs/reference/env-vars.md for definitions."
        )


__all__ = [
    "AGENT_EXTENSIONS",
    "AGENT_FOUNDRY_ACCEPTANCE_SCENARIOS",
    "AGENT_FOUNDRY_PROJECT_ROOT",
    "AGENT_FOUNDRY_REQUIRE_LIVE",
    "AGENT_FOUNDRY_RUN_ID",
    "AGENT_FOUNDRY_TARGET",
    "AGENT_FOUNDRY_TELEMETRY_DISABLED",
    "AGENT_FOUNDRY_TEST_CATALOG",
    "AGENT_FOUNDRY_TEST_GATEWAY_SKILL",
    "AGENT_FOUNDRY_TEST_MODEL_SERVICE",
    "AGENT_FOUNDRY_TEST_VS_ENDPOINT",
    "AGENT_MEMORY_MAX_TOPIC_CHARS",
    "AGENT_MODULE",
    "API_PROXY",
    "CHAT_APP_PORT",
    "CHAT_HISTORY_RETENTION_DAYS",
    "CHAT_HISTORY_SIDEBAR_DAYS",
    "CHAT_MAX_WIDTH",
    "CHAT_PROXY_TIMEOUT_SECONDS",
    "CHAT_UI_OVERLAYS",
    "COMPACTION_INSTRUCTIONS",
    "COMPACTION_KEEP_TURNS",
    "COMPACTION_MANUAL_ACTION",
    "COMPACTION_TRIGGER_TOKENS",
    "Context",
    "DATABRICKS_APP_NAME",
    "DATABRICKS_AUTH_TYPE",
    "DATABRICKS_CLIENT_ID",
    "DATABRICKS_CLIENT_SECRET",
    "DATABRICKS_CONFIG_PROFILE",
    "DATABRICKS_HOST",
    "DATABRICKS_TOKEN",
    "DATABRICKS_WAREHOUSE_ID",
    "DEPLOY_TARGET",
    "ENABLE_A2A",
    "ENABLE_AGENT_MEMORY",
    "ENABLE_CHARTS",
    "ENABLE_REPORTS",
    "ENABLE_CHAT_PLAN",
    "ENABLE_COMPACTION",
    "ENABLE_FEEDBACK",
    "ENABLE_FILE_UPLOAD",
    "ENABLE_IMAGE_INPUT",
    "ENABLE_MCP_SERVER",
    "ENABLE_RESOURCES_SIDEBAR",
    "ENABLE_SPEECH_INPUT",
    "ENABLE_USER_SKILLS",
    "EVAL_HTTP_TIMEOUT_SECONDS",
    "EnvVar",
    "FILE_UPLOAD_MAX_CONCURRENCY",
    "FILE_UPLOAD_MAX_MB",
    "FILE_UPLOAD_MEM_LIMIT_MB",
    "FILE_UPLOAD_PORT",
    "INSTANCE_DIR",
    "MCP_CLIENT_SESSION_TIMEOUT_SECONDS",
    "MCP_TOOLS_CACHE_TTL_SECONDS",
    "MLFLOW_EXPERIMENT_ID",
    "MLFLOW_TRACING_SQL_WAREHOUSE_ID",
    "MissingEnvVar",
    "PLAN_MAX_CHARS",
    "PORT",
    "REGISTRY",
    "SPEECH_BACKEND",
    "SPEECH_ENDPOINT",
    "SPEECH_LANGUAGE",
    "SPEECH_MAX_SECONDS",
    "TEST_WAREHOUSE_ID",
    "TITLE_MODEL_ENDPOINT",
    "TOOL_OUTPUT_EXPANDED",
    "AGENT_CATALOG",
    "AGENT_SCHEMA",
    "AGENT_SCHEMA_PREFIX",
    "TRACING_UC_DESTINATION",
    "USER_SKILLS_MAX",
    "USER_SKILLS_MAX_MD_CHARS",
    "on_databricks_apps",
    "read",
    "read_bool",
    "read_int",
    "read_optional",
    "render_docs",
    "validate_for",
    "vars_for",
]


# ---------------------------------------------------------------------------
# Documentation generator (kept here so the registry has a single source
# of truth for both code and docs -- issue #155)
# ---------------------------------------------------------------------------

_DOCS_HEADER = (
    "# Environment variables\n"
    "\n"
    "<!-- AUTO-GENERATED from agent_foundry/env.py REGISTRY.\n"
    "     Do not edit by hand; run `python -m agent_foundry.env --generate-docs`. -->\n"
    "\n"
    "The framework's runtime / deploy / app contracts are expressed via the "
    "environment variables listed below.  The single source of truth is "
    "``agent_foundry.env.REGISTRY``; this document is generated "
    "from it and kept in sync by a unit test.\n"
    "\n"
    "## Contexts\n"
    "\n"
    "- **deploy** -- the ``agent-foundry deploy`` CLI\n"
    "- **runtime** -- the deployed agent process (Model Serving, Apps backend)\n"
    "- **app** -- the chat-UI App container (``app.yaml``)\n"
    "- **eval** -- the local/CI evaluation harness\n"
    "\n"
    "Variables marked _optional_ have no required context; they tune behaviour "
    "but are not part of any boundary contract.\n"
    "\n"
    "## Registry\n"
    "\n"
    "| Name | Required at | Default | Description |\n"
    "| ---- | ----------- | ------- | ----------- |\n"
)

_DOCS_FOOTER = (
    "\n"
    "## Reading values in code\n"
    "\n"
    "```python\n"
    "from agent_foundry import env\n"
    "\n"
    "# Required (raises MissingEnvVar when no value and no default):\n"
    "root = env.read(env.AGENT_FOUNDRY_PROJECT_ROOT)\n"
    "\n"
    "# Optional:\n"
    "profile = env.read_optional(env.DATABRICKS_CONFIG_PROFILE)\n"
    "\n"
    "# Typed parsers:\n"
    "port = env.read_int(env.CHAT_APP_PORT, fallback=3000)\n"
    "enabled = env.read_bool(env.ENABLE_MCP_SERVER)\n"
    "\n"
    "# Fail-fast at a context boundary:\n"
    'env.validate_for("runtime")  # raises listing every missing var\n'
    "```\n"
)


def _row(var: EnvVar) -> str:
    contexts = ", ".join(var.required_at) if var.required_at else "_optional_"
    default = f"`{var.default}`" if var.default is not None else "_(none)_"
    sensitive = " [secret]" if var.sensitive else ""
    return f"| `{var.name}`{sensitive} | {contexts} | {default} | {var.description} |"


def render_docs() -> str:
    """Render the full ``docs/reference/env-vars.md`` body from :data:`REGISTRY`.

    Used by ``python -m agent_foundry.env --generate-docs`` and by
    :mod:`agent_foundry.tests.test_env_docs_sync` to enforce that the
    on-disk file never drifts from the in-memory registry.
    """
    table = "\n".join(_row(v) for v in REGISTRY)
    return f"{_DOCS_HEADER}{table}\n{_DOCS_FOOTER}"


def _docs_main(argv: list[str] | None = None) -> int:
    import argparse
    from pathlib import Path

    parser = argparse.ArgumentParser(description="Generate or check docs/reference/env-vars.md from REGISTRY.")
    parser.add_argument(
        "--generate-docs",
        action="store_true",
        help="Write docs/reference/env-vars.md from the registry.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit 1 if docs/reference/env-vars.md does not match the registry.",
    )
    args = parser.parse_args(argv)
    if not (args.generate_docs or args.check):
        parser.error("specify --generate-docs or --check")

    body = render_docs()
    docs_path = Path(__file__).resolve().parent.parent / "docs" / "reference" / "env-vars.md"

    if args.check:
        try:
            current = docs_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            print(f"docs/reference/env-vars.md missing at {docs_path}")
            return 1
        if current != body:
            print(
                "docs/reference/env-vars.md is out of sync with agent_foundry/env.py. "
                "Run `python -m agent_foundry.env --generate-docs`."
            )
            return 1
        return 0

    docs_path.parent.mkdir(parents=True, exist_ok=True)
    docs_path.write_text(body, encoding="utf-8")
    print(f"Wrote {docs_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_docs_main())
