# SQL Function Instance

Copy this template to `instances/<app>/sql_functions/<name>/` and
customise.

## Adding functions

Place `.sql` files in the `functions/` directory.  Each file should
contain one or more `CREATE OR REPLACE FUNCTION` statements naming the
function as:

```sql
CREATE OR REPLACE FUNCTION `${var.catalog}`.`${var.agent_schema_name}`.<function_name>(
```

Compose the two parts rather than using `${var.agent_schema}` in SQL.
`agent_schema` is already `catalog.schema`, so it cannot be back-quoted as
a unit, and an unquoted identifier fails whenever the catalog or schema
name contains a hyphen -- which Unity Catalog allows.

Prefix filenames with numbers (`01_`, `02_`) to control execution order
when functions depend on each other.

## COMMENT convention

Always add `COMMENT` to both the function and every parameter.  The
agent LLM relies on these descriptions to decide when and how to call
the tool.

## Wiring to the agent

Expose the functions through a `managed_functions` MCP server,
`payloads/agent/mcp_servers/<name>/server.yml`:

```yaml
type: managed_functions
catalog: "${var.catalog}"
schema: "${var.schema_prefix}_agent"
tools:
  allow:
    - match: "*__*__example_fn"
      expose_as: example_fn
```

A skill's `SKILL.md` can then tell the model when to call `example_fn`.
