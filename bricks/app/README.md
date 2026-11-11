# App Brick — Bundled Agent + Chat UI

> **Technical reference** for deployers and developers.

Deploys the agent backend and the official Databricks
[`e2e-chatbot-app-next`](https://github.com/databricks/app-templates/tree/main/e2e-chatbot-app-next)
chat UI as a **single Databricks App**, with optional
**MCP** and **A2A** endpoints for programmatic / inter-agent access.

## Instance layout & payload adapter

The user-facing brick is **`app`** (the package directory is still
`bricks/app/`). `app/` is a **generic host shell**; the agent runtime
surface is **payload-owned** under `payloads/agent/`:

| File | Owns |
|------|------|
| `app/config.yml` | App **infrastructure**: compute size, resource bindings, OBO scopes, SP grants, and the `payloads` host selection (typed by `AgentAppConfig`) |
| `app/app.yaml` | Optional low-level container `command`/`env` override (infra env only; not shipped by default) |
| `payloads/agent/payload.yml` | The **payload**: which adapter runs (`payload.adapter`) + an adapter-owned `spec` — the **single source** for the agent's runtime config (LLM, prompt, limits, tools), UI feature toggles (`chat_history`, `file_upload`, `feedback`, `charts`) and presentation modes (`tool_output`), and integrations (`a2a`, `mcp_server`). Typed by `payloads.agent.contracts.AgentPayloadFile` |
| `payloads/agent/skills/` · `payloads/agent/mcp_servers/` | Agent skill bundle and MCP server registry |

The App host (`agent_foundry/deploy/app_host.py`) owns the generic App
lifecycle; a **payload adapter** translates its own `spec` into a neutral
`AppPayloadSpec`. The only built-in adapter today is `agent` (this brick).
When `payloads/agent/payload.yml` is absent the App deploys the `agent`
payload with framework defaults. The framework itself is **payload-agnostic**:
it knows only the generic `payload.adapter` + opaque `spec` envelope, never
what an "agent" is.

## Architecture

```
                   ┌──────────────────────────────────────────────────────┐
                   │  Single Databricks App  (port 8000)                  │
                   │                                                      │
  Browser ──────►  │  AgentServer (enable_chat_proxy=True)                │
                   │    ├── /invocations, /responses  → agent             │
                   │    └── /*  → proxy to chat UI (port 3000)            │
                   │                                                      │
  MCP Client ───►  │  /api/mcp/   → FastMCP (ask_agent tool)             │
                   │                                                      │
  A2A Client ───►  │  /api/a2a    → A2A JSON-RPC (streaming artifacts)   │
                   │  /api/.well-known/agent-card.json                    │
                   │                                                      │
                   │  e2e-chatbot-app-next  (port 3000)                   │
                   │    └── API_PROXY=localhost:8000/invocations           │
                   │                                                      │
                   │  file-extractor  (port 8001, optional, isolated)     │
                   │    └── POST /extract  (only if ENABLE_FILE_UPLOAD)   │
                   └──────────────────────────────────────────────────────┘
```

The `start_app.py` process manager launches both the Python agent backend
and the Node.js chat frontend.  The MLflow `AgentServer` with
`enable_chat_proxy=True` proxies UI traffic, so external users only
interact with port 8000.  When `ENABLE_FILE_UPLOAD` is set it also
supervises a third, **isolated** file-extraction process (port 8001) —
see [File upload (extraction)](#file-upload-extraction).

All `/api/*` routes benefit from the Databricks Apps OAuth proxy, which
enforces authentication and injects **on-behalf-of (OBO)** headers
(`x-forwarded-access-token`, `x-forwarded-preferred-username`) so the
agent runs with the caller's identity.

## MCP & A2A Integration

The agent can be invoked programmatically via two open protocols,
enabled by setting environment variables in the instance `app.yaml`:

```yaml
env:
  - name: ENABLE_MCP_SERVER
    value: "true"
  - name: ENABLE_A2A
    value: "true"
```

### Model Context Protocol (MCP)

- **Endpoint**: `/api/mcp/` (trailing slash required)
- Exposes a single `ask_agent` tool via `FastMCP`
- Uses JSON responses (not SSE) for compatibility with the Databricks
  Apps proxy — see `mcp_adapter.py` for details
- Non-streaming: returns the full agent response as a single JSON result

### Agent-to-Agent Protocol (A2A)

- **Endpoint**: `/api/a2a` (JSON-RPC over SSE)
- **Agent card**: `/api/.well-known/agent-card.json`
- **Streaming**: the agent card advertises `streaming=True`; artifact
  chunks are emitted incrementally as the agent generates its response
  via `Runner.run_streamed()`
- Uses the `a2a-sdk` with `DefaultRequestHandler` and `InMemoryTaskStore`

#### Agent card configuration

The card served at `/api/.well-known/agent-card.json` is authored per
instance via the `spec.integrations.a2a` block in
`payloads/agent/payload.yml`. This describes what **external A2A consumers**
(other agents, A2A clients) see -- it is deliberately separate from the
internal `payloads/agent/skills/` directory, which describes what this agent
uses *internally* to fulfil a request. See
[`agent_foundry/contracts/a2a.py`](../../agent_foundry/contracts/a2a.py)
for the full card schema and validation rules.

**Exposure toggle + content in one place.** Both the mount toggle
(`spec.integrations.a2a.enabled` → derives `ENABLE_A2A`) and the card
*content* (`spec.integrations.a2a.card`) live in the payload `spec`. The
endpoint is served with a non-fallback card only when `enabled: true` and a
`card` is authored.

Minimal example:

```yaml
# instances/<name>/payloads/agent/payload.yml
payload:
  adapter: agent
  spec:
    integrations:
      a2a:
        enabled: true
        card:
          name: "My Agent"
          description: "What this agent does, from an external client's point of view."
          version: "1.0.0"
          capabilities:
            streaming: true
          skills:
            - id: "ask_anything"
              name: "Ask Anything"
              description: "Natural-language Q&A over the agent's knowledge."
              tags: ["general"]
              examples: ["What is the revenue for Q4?"]
```

Validation: loose semver `version`, non-empty `skills`, unique skill
`id` per card. Card fields placed directly under `a2a:` (instead of
under `a2a.card:`) are rejected with a friendly migration hint.

When the `card` is absent the brick falls back to a generic default card
and logs a WARNING. See
[`examples/portfolio_assistant/payloads/agent/payload.yml`](../../examples/portfolio_assistant/payloads/agent/payload.yml)
for a realistic two-skill card.

### Shared Agent Runner

Both adapters delegate to `agent_runner.py`, which centralises the full
agent lifecycle (init → build tools → connect MCP servers → create agent
→ run).  Two execution modes:

| Function | Mode | Used by |
|----------|------|---------|
| `run_agent()` | Blocking, returns full text | MCP adapter |
| `run_agent_streamed()` | Async generator, yields per-message chunks | A2A adapter |

OBO identity is propagated via `contextvars`: each adapter captures
incoming request headers and stores them before invoking the runner.

### Local Caller Scripts

See [`docs/how-to/integrate-mcp-a2a.md`](../../docs/how-to/integrate-mcp-a2a.md)
and [`agent_foundry/cli/call.py`](../../agent_foundry/cli/call.py).
The two callers are exposed as
`uv run agent-foundry call mcp ...` and
`uv run agent-foundry call a2a ...` (install the optional `[call]`
extra: `uv pip install 'agent-foundry[call]'`).

## Usage

### Instance setup

The agent runtime surface is payload-owned under `payloads/agent/`; `app/`
holds only the host infrastructure:

```
instances/my-agent/
├── foundry.toml
├── app/
│   ├── config.yml      ← App infrastructure (compute, resources, OBO scopes, payload ref)
│   └── app.yaml        ← OPTIONAL infra-env override (not shipped by default)
├── payloads/agent/
│   ├── payload.yml     ← payload adapter + spec (runtime + ui + integrations: the single config)
│   ├── skills/
│   └── mcp_servers/
└── ...
```

The optional instance `app.yaml` can extend the base template with additional env
vars (e.g. `MLFLOW_TRACING_DESTINATION`).  All `${var.*}` placeholders
are resolved at deploy time.

### Deploy

Via the orchestrator:

```bash
uv run agent-foundry deploy --target dev --instance my-agent
```

Or standalone:

```bash
uv run agent-foundry brick app deploy \
    --app-name my-agent-app \
    --target dev \
    --instance my-agent
```

### Chat history (Lakebase)

Persistent chat history is a UI feature of the agent payload. It is
authored in the payload `spec.ui.chat_history` block in
`payloads/agent/payload.yml` (the single per-payload config contract):

```yaml
# Default-on: a fresh `agent-foundry init` project ships with a
# working chat UI + Lakebase-backed history out of the box.
payload:
  adapter: agent
  spec:
    ui:
      chat_history:
        enabled: true
        # project_name: "my-agent-lakebase"  # optional; defaults to <target>-<app>-lakebase
```

When `enabled = true`, `app deploy` provisions the Lakebase
Autoscaling project before creating the App.  The discovered project,
branch, and database paths are written to
`/Shared/agent-foundry/<instance>/_state/lakebase/<instance>/<target>.json` and bound as the App's
`postgres` resource (`CAN_CONNECT_AND_CREATE`).  When
`enabled = false`, provisioning is skipped *and* the `postgres`
entry is filtered from the App's resource list -- the App is never
bound to a project that was not provisioned.

There is no separate `bricks/lakebase/` brick: the legacy
`agent-foundry brick lakebase deploy` CLI verb was removed in the
PR that merged Lakebase into the Agent App.

### File upload (extraction)

Lets a user attach a file in the chat composer and have its contents put
in front of the agent **immediately** — the file is parsed to text/structured
data server-side and injected into the conversation. It is controlled by the
agent payload's `spec.ui.file_upload` block in `payloads/agent/payload.yml`
(the single per-payload config contract):

```yaml
payload:
  adapter: agent
  spec:
    ui:
      # Default-OFF: opt-in feature (untrusted-input parser).
      file_upload:
        enabled: true
        max_mb: 10      # hard upload cap (default 10, must be > 0)
```

**Supported formats** (v1): `xlsx`, `csv`, `docx`, `pdf` (text-only — no OCR),
`txt`, `md`, `json`. Unsupported types are rejected with a clear message.
Extracted text is truncated to a per-file budget (≈50k chars / 2k rows) with an
explicit notice; tune via `FILE_UPLOAD_*` env vars (see
[`docs/reference/env-vars.md`](../../docs/reference/env-vars.md)).

**Isolation (why a third process).** A single user's malformed, oversized, or
malicious file must never be able to take down the agent. When the toggle is on,
`app deploy` folds the `extraction` wheel extra (the parser libs +
`fastapi`/`uvicorn`) into the staged requirements, and `start_app.py` launches
the extractor as a **supervised, isolated child** on port 8001. A crash/OOM/
exploit while parsing only kills the disposable worker — the extractor is
restarted with bounded backoff and never fails the app. Each parse additionally
runs in a `spawn`ed subprocess with a kill-on-timeout and optional memory
ceiling (`FILE_UPLOAD_MEM_LIMIT_MB`), and XML/zip defenses (`defusedxml`,
`guard_zip`) run before any document parse. Treat this process as
untrusted-input-facing in security review.

**Flow.** Composer → `POST /api/files/upload` (Node `files.ts`, auth +
OBO-forwarded) → `POST /extract` (extractor :8001) → extracted text returned
synchronously and persisted as hidden attachment metadata. The user sees an
attachment chip; the extracted text is injected into the *model-bound* messages
(never re-rendered in the transcript) on every turn, so it survives multi-turn
chat via Lakebase history. The shared parsing core lives in
[`payloads.agent.runtime.extraction`](../../payloads/agent/runtime/extraction/) so
the same logic is reusable in-process.

When `enabled = false` (the default) the extractor is never started, the
`extraction` extra is not installed, and the client hides the attach affordance.

### Local development

```bash
DATABRICKS_CONFIG_PROFILE=my-profile \
    uv run agent-foundry run --instance my-agent
```

Open http://localhost:8000 for the chat UI.  Pass `--no-ui` to run the
agent backend only, or `--reload` to auto-reload on code changes.

## Files

| File | Purpose |
|------|---------|
| `app.yaml` | Template: startup command + env vars |
| `scripts/start_app.py` | Process manager (backend + frontend) |
| `scripts/deploy_agent_app.py` | Deploy via Databricks SDK Apps API |
| `brick.py` | Deploy-time adapter (`AppBrick`) |

The in-container runtime entrypoints `start_server.py`, `extractor_service.py`
and `extract_worker.py` live under `payloads/agent/runtime/`; `start_app.py` spawns them by module path. The `app` brick remains the
generic host that boots the selected payload's runtime.

## Known Limitations

- MCP `ask_agent` is **single-turn** (no conversation history).
  Multi-turn requires A2A with context IDs.
- MCP uses JSON responses instead of SSE due to the Databricks Apps
  proxy re-encoding SSE streams on `/api/*` routes. This means no
  streaming for MCP — the full response is returned at once.
- A2A uses `InMemoryTaskStore` — task state is lost on app restart.
- A2A streaming is at **message-level granularity**: each complete
  assistant message is emitted as one artifact chunk (not token-by-token).
