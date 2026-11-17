# Unity Gateway Assistant (example)

A full-feature agent whose model and most of its tools run through **Unity AI
Gateway**, next to one workspace MCP server. Use it as the starting point when
you want the LLM endpoint, tools, and skills governed in Unity Catalog instead
of wired into the App.

It needs the Unity AI Gateway previews enabled in your workspace.

## What gets deployed

| Piece | What it is | Where |
| --- | --- | --- |
| **Agent App** | Chat UI and agent on the `agent` payload, with chat history, file and image upload, charts, feedback, and the resources panel | `app/config.yml`, `payloads/agent/payload.yml` |
| **Model** | Claude Sonnet 5.5 as a Unity Gateway `model_service` (`system.ai.claude-sonnet-5-5`); deploy grants the App `EXECUTE` on it | `payloads/agent/payload.yml` |
| **Gateway tools** | Web search, called as the App, and Genie One, called as the signed-in user so it sees only what that user may read | `payloads/agent/payload.yml` |
| **Workspace tool** | Read-only SQL on the instance's warehouse, as the signed-in user | `payloads/agent/mcp_servers/sql_warehouse/server.yml` |
| **Skill** | `sql-runner`, a git-managed skill that `ai_gateway` deploys to a Unity Catalog schema; the agent pins it from there | `ai_gateway/` |
| **Compute** | Dedicated X-Small SQL warehouse, which tracing and observability need | `warehouse/config.yml` |
| **Tracing and dashboard** | MLflow tracing and a Lakeview operations dashboard, from `payload.requires` | `payloads/agent/payload.yml` |
| **Tests** | Instance unit tests and a post-deploy smoke question that checks web search | `tests/`, `smoke.yml` |

## What the agent can do

- **Search the web** and cite each source as a Markdown link.
- **Answer questions about your data** with Genie One or exact read-only SQL,
  always as the user, never as the App.
- **Use more gateway services.** Users add other `system.ai` services (GitHub,
  Slack, Gmail, and so on) from the resources panel; each runs as that user.
- **Keep their own skills.** Users write, edit, and pick skills in the
  resources panel; they live in the chat-history Lakebase.

The payload turns on most context and UI options: prompt caching, tool-result
clearing, compaction with a manual action, plan, long-term memory, and
adaptive reasoning. The
[capabilities guide](../../payloads/agent/CAPABILITIES.md) explains each one.

## Deploy

```bash
agent-foundry deploy --target dev
```

## Make it yours

| Change | Where |
| --- | --- |
| Model | `payloads/agent/payload.yml` `spec.runtime.model` |
| Pinned gateway tools and whether they run as the App (`sp`) or the user (`obo`) | `payloads/agent/payload.yml` `tools.mcp.gateway.pinned` |
| Add a skill | a folder under `ai_gateway/skills/`, then pin it in `payloads/agent/payload.yml` |
| Skills schema | `ai_gateway/config.yml` |
| Warehouse size and auto-stop | `warehouse/config.yml` |
