# Integrate MCP & A2A (Provider side)

> **Scope:** This guide covers the **provider** side -- how the agent exposes
> itself to external callers via the Model Context Protocol (MCP) and the
> Agent-to-Agent (A2A) protocol. For the **consumer** side -- how the agent
> calls MCP servers as tool sources -- see the
> [MCP capability](../../payloads/agent/deploy/mcp/README.md).

> **Status:** Public Preview
> **Last Updated:** April 2026
> **Protocols:** MCP (`/api/mcp/`), A2A (`/api/a2a`)

---

## 1. What & Why

The chat UI is only one entry point to the agent. Any agent deployed via the
[`app` brick](../../bricks/app/README.md) can also be reached
programmatically through two open protocols:

| Entry point | Best for |
|-------------|----------|
| Chat UI | Interactive users, long-running sessions, human feedback |
| MCP (`/api/mcp/`) | One-shot calls from scripts, notebooks, AI Playground, other LLMs |
| A2A (`/api/a2a`) | Multi-turn conversations between agents, streaming orchestration |

Both protocol endpoints live under `/api/*` so the Databricks Apps platform
enforces OAuth automatically and the agent runs with the **caller's**
identity, not the app service principal's.

---

## 2. MCP vs A2A -- when to pick each

| Dimension | MCP (`/api/mcp/`) | A2A (`/api/a2a`) |
|-----------|-------------------|------------------|
| Semantics | Single tool call (`ask_agent`) | Full agent task with lifecycle |
| Turns | Single-turn (no history) | Multi-turn via `context_id` |
| Transport | JSON response (non-streaming) | JSON-RPC over SSE (streaming) |
| Discovery | MCP tool list | `/api/.well-known/agent-card.json` |
| Output granularity | Full response in one result | Per-message artifact chunks |
| Ideal caller | AI Playground, notebook cell, cron script | Another agent, orchestrator, long-running workflow |

Pick **MCP** for the simplest possible call pattern ("ask this agent one
question, get one answer"). Pick **A2A** when the caller needs to stream
partial output, maintain conversation state, or chain multiple turns.

---

## 3. Enablement

Both endpoints are **opt-in** via the agent payload's `spec.integrations`
block. Set them in your instance's `payloads/agent/payload.yml` — the deploy
derives the `ENABLE_MCP_SERVER` / `ENABLE_A2A` env from these, so you no
longer author them in `app/app.yaml`:

```yaml
payload:
  adapter: agent
  spec:
    integrations:
      mcp_server:
        enabled: true     # expose the agent AS an MCP server at /api/mcp/
      a2a:
        enabled: true     # expose the agent via an A2A card at /api/a2a
```

`spec.integrations` is the **exposure** half of these protocols (the agent
serving itself to other systems). The **consumption** half — the agent
*using* external MCP servers as tool sources — is presence-based via the
`payloads/agent/mcp_servers/` directory and has no toggle here.

[`payloads/agent/runtime/start_server.py`](../../payloads/agent/runtime/start_server.py)
reads the derived `ENABLE_*` flags and conditionally mounts the MCP and A2A sub-applications on
the same FastAPI server that hosts `/invocations` and the chat UI proxy.
Neither flag has any side effect when disabled.

---

## 4. MCP Endpoint

- **URL:** `https://<app-host>/api/mcp/` (trailing slash required)
- **Tool exposed:** `ask_agent(question: str) -> str`
- **Implementation:** [`payloads/agent/runtime/mcp_adapter.py`](../../payloads/agent/runtime/mcp_adapter.py)
  using `FastMCP`
- **Transport:** JSON responses. SSE is intentionally disabled because the
  Databricks Apps OAuth proxy re-encodes SSE streams on `/api/*` routes,
  which breaks MCP stream parsing. The full agent answer is returned as a
  single JSON result.

### Execution path

```
MCP client ──► /api/mcp/ ──► FastMCP ──► ask_agent tool
                                            │
                                            ▼
                              agent_runner.run_agent()
                                            │
                                            ▼
                        prepare context → build tools → Runner.run()
```

See
[`payloads/agent/runtime/agent_runner.py`](../../payloads/agent/runtime/agent_runner.py)
for the shared runner. `run_agent()` is the blocking variant used by MCP.

---

## 5. A2A Endpoint

- **URL:** `https://<app-host>/api/a2a` (JSON-RPC over SSE)
- **Agent card:** `https://<app-host>/api/.well-known/agent-card.json`
- **SDK:** `a2a-sdk` with `DefaultRequestHandler` and `InMemoryTaskStore`
- **Implementation:** [`payloads/agent/runtime/a2a_adapter.py`](../../payloads/agent/runtime/a2a_adapter.py)
  (`FoundryAgentExecutor`)

### Discovery

Callers fetch the agent card to learn the endpoint, declared skills, and
streaming capabilities before opening a task:

```json
{
  "name": "Foundry Agent",
  "description": "Databricks AI Agent exposed via A2A protocol",
  "capabilities": {"streaming": true},
  "skills": [
    {"id": "ask_agent", "name": "Ask Agent", "description": "Ask the agent a question"}
  ]
}
```

### Streaming

The executor calls `agent_runner.run_agent_streamed()` which yields
**message-level** chunks. Each complete assistant message surfaces as one
`TaskArtifactUpdateEvent`, interleaved with
`TaskStatusUpdateEvent`s for lifecycle transitions
(`TASK_STATE_WORKING` → `TASK_STATE_COMPLETED`).

Tool calls and intermediate reasoning are **not** emitted as artifacts --
only final text content the user should see.

### Multi-turn

Clients maintain continuity by passing a stable `context_id` across requests.
Task state is kept in `InMemoryTaskStore`, so state is lost on app restart.
Persistent multi-turn storage is a future enhancement (see Known
limitations).

---

## 6. OAuth and OBO Identity

All `/api/*` routes are protected by the Databricks Apps OAuth proxy.
Authenticated callers receive two forwarded headers:

| Header | Purpose |
|--------|---------|
| `x-forwarded-access-token` | OAuth access token of the end-user |
| `x-forwarded-preferred-username` | Human-readable username |

The MCP and A2A adapters capture these headers in middleware and stash them
into a `contextvars` `ContextVar` before invoking `agent_runner.run_agent()`.
The runner and its tools then use those headers for **on-behalf-of (OBO)**
calls to Databricks services (Genie, Vector Search, UC Functions), so
authorization reflects the calling user -- not the app service principal.

If you build a custom caller, no additional work is needed: attach a valid
Databricks OAuth bearer token and the proxy injects the `x-forwarded-*`
headers automatically.

---

## 7. Calling Patterns

Two CLI subcommands wrap the protocol-specific clients end-to-end. Install
the optional `[call]` extra to pull in `databricks-mcp`, `a2a-sdk`, and
`httpx`:

```bash
uv pip install 'agent-foundry[call]'
```

| Verb | Pattern |
|------|---------|
| `agent-foundry call mcp` | `DatabricksMCPClient` connecting to `/api/mcp/`, calling `ask_agent` |
| `agent-foundry call a2a` | `ClientFactory` + `A2ACardResolver`, streaming task events from `/api/a2a` |

The implementations live in
[`agent_foundry/cli/call.py`](../../agent_foundry/cli/call.py) and are also
importable as `from agent_foundry.cli.call import call_a2a, call_mcp` for
use from notebooks, CI jobs, or other services.

---

## 8. Inter-App Permissions

When the caller is **another Databricks App** (not a local script signed
in as a human user), its service principal needs `CAN_USE` permission on
the agent app:

```bash
databricks apps update-permissions <agent-app-name> \
    --json '{
      "access_control_list": [{
        "service_principal_name": "<caller-sp>",
        "permission_level": "CAN_USE"
      }]
    }'
```

Local scripts authenticated via `databricks auth login` do not need this
grant -- the caller's own identity is used.

---

## 9. Known Limitations

- **MCP is single-turn.** `ask_agent` takes one question and returns one
  answer. There is no conversation history. Use A2A for multi-turn.
- **MCP is non-streaming.** The Databricks Apps proxy re-encodes SSE on
  `/api/*`, so MCP responses are JSON only. The entire answer is returned
  at once.
- **A2A task store is in-memory.** Task state lives in `InMemoryTaskStore`
  and is lost on app restart. Durable storage would require a custom
  `TaskStore` (e.g. backed by Lakebase).
- **A2A streaming is message-level.** Each complete assistant message is
  yielded as one artifact chunk -- not token-by-token. Tool calls and
  intermediate reasoning are filtered out.
- **No AI Playground auto-discovery.** By design -- these endpoints target
  programmatic callers, not the Playground UI.

---

## 10. Resources and References

- [`bricks/app/README.md`](../../bricks/app/README.md) -- technical
  reference for the agent app brick (implementation source of truth)
- [`payloads/agent/runtime/agent_runner.py`](../../payloads/agent/runtime/agent_runner.py)
  -- shared runner with `run_agent()` and `run_agent_streamed()`
- [`payloads/agent/runtime/mcp_adapter.py`](../../payloads/agent/runtime/mcp_adapter.py)
  -- MCP endpoint with `ask_agent` tool
- [`payloads/agent/runtime/a2a_adapter.py`](../../payloads/agent/runtime/a2a_adapter.py)
  -- A2A endpoint (`FoundryAgentExecutor`)
- [`agent_foundry/cli/call.py`](../../agent_foundry/cli/call.py) -- the
  `call_a2a` and `call_mcp` functions used by `agent-foundry call`
- [MCP specification](https://modelcontextprotocol.io/introduction)
- [A2A specification](https://a2aproject.github.io/A2A/)
- [Databricks Apps OAuth proxy](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/auth)
