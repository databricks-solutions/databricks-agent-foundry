---
name: my-skill
description: Replace with what this skill does and when to use it. Use when the user asks about X or needs Y.
---
# My skill

Copy this directory to `payloads/agent/skills/<skill-id>/` and set `name`
above to the directory name: lowercase letters, digits and single hyphens.
The agent decides when to use the skill from `description` alone, so write
it as "what it does. Use when ...", at most 1024 characters.

The body is free markdown with no required headings. It is what the agent
follows once the skill applies. Tools are not declared here: they come from
the MCP servers in `mcp_servers/`, so refer to them by name in prose, e.g.
"call `sql_warehouse__execute_sql_read_only` with the query".

## Workflow

1. Step one.
2. Step two.

## Edge cases

- **No data found:** say so and suggest how to narrow or widen the request.
