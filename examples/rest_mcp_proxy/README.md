# REST-to-MCP Proxy (example)

A **config-only** Agent Foundry instance that turns an external REST API into a
governed [MCP](https://modelcontextprotocol.io) server, hosted on a Databricks
App. There is no agent here — the App *is* the MCP server, and any Databricks
agent (or the Assistant) can attach to it.

This example fronts the **public GitHub REST API**, which needs no credentials
for public data, so it deploys and works end-to-end with zero secrets.

## What gets deployed

- **One Databricks App** running the `rest_mcp` payload (a Streamable-HTTP MCP
  server). Each `api` in `payloads/rest_mcp/payload.yml` is mounted at
  `/mcp/<name>` — here, `/mcp/github`.
- **One governed Unity Catalog `is_mcp` connection per API**, created at deploy
  time, pointing at the App mount with an OAuth U2M mapping. Agents reach the
  tools through Unity AI Gateway, so the agent never handles an upstream token.
  The connection name is published as `${var.rest_mcp_connection__github}`.

## The config contract

Everything lives in one file: [`payloads/rest_mcp/payload.yml`](payloads/rest_mcp/payload.yml).
Each `tool` declaration is the single source of truth for three artifacts that
can never drift: the MCP `inputSchema`, the upstream HTTP request, and the
response projection.

The example shows the two common declarative shapes:

| Tool | Demonstrates |
| --- | --- |
| `get_repository` | path args (`/repos/{owner}/{repo}`) + response projection |
| `search_repositories` | query args (`q`, `sort`, `per_page`) + projecting a nested result set |

To point at a different API, change `rest.base_url` / `rest.allowed_hosts` and
rewrite the `tools`. For an authenticated API, store a token in a Databricks
secret and set `rest.auth` to `mode: static` (see the commented block in the
payload manifest).

## Deploy

```bash
agent-foundry deploy --target dev
```

## Consume it from an agent

An agent instance attaches to the proxy with an `external` MCP server that
references the connection this deploy creates:

```yaml
# instances/<agent>/payloads/agent/mcp_servers/github/server.yml
type: external
connection_name: "${var.rest_mcp_connection__github}"
description: "GitHub REST API via the rest_mcp proxy"
tools:
  allow:
    - get_repository
    - search_repositories
```
