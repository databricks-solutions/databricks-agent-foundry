# Starters

`agent-foundry init` asks which **starter** your application repo begins with.
`blank` is an empty instance. The others are complete, working instances,
self-contained and covered by their own tests: you deploy one as it is, then
adapt it. They are end-to-end solutions, not minimal brick starters (those
live under `bricks/<brick>/_scaffold/`).

## Choose a starter

| Starter | What you get | What it deploys | Pick it when |
| --- | --- | --- | --- |
| [`portfolio_assistant`](portfolio_assistant/README.md) | A production reference agent: a data assistant over Unity Catalog metadata and a documentation corpus, with the universe around it | Agent App, Genie space, Vector Search index with an ingestion job, curated data and UC functions, tracing, evaluation and monitoring, Lakeview dashboard | You want the whole production setup to start from or learn from |
| [`unity_gateway_assistant`](unity_gateway_assistant/README.md) | A full-feature agent whose model, web search, and Genie run through Unity AI Gateway | Agent App, SQL warehouse, one git-managed skill, tracing and dashboard | You build on Unity AI Gateway (needs its previews enabled) |
| [`rest_mcp_proxy`](rest_mcp_proxy/README.md) | A config-only REST-to-MCP proxy, no agent | One App, one Unity Catalog MCP connection | You want agents to call a REST API as governed MCP tools |
| `blank` | An empty instance | Nothing until you scaffold bricks | You assemble your own with `agent-foundry scaffold` |

## For maintainers

### Adding a new variant

1. Create `examples/<variant_id>/` with the same shape as an existing variant
   (`foundry.toml`, per-brick subdirs, optional `tests/`).
2. Add `examples/<variant_id>/__init__.py` (empty package marker for wheel
   packaging).
3. Add `examples/<variant_id>/manifest.toml`:

   ```toml
   display_name = "My Example"
   description = "One-line summary shown in the init wizard"
   ```

4. Add `examples/<variant_id>/README.md` and a row in the table above.
5. Run `uv run pytest tests/contracts/test_examples_packaging.py agent_foundry/tests/unit/init/test_examples_registry.py` —
   CI asserts the variant is on disk with `__init__.py` + `manifest.toml` and that
   `pyproject.toml` already ships all of `examples/` via the parent
   `[tool.setuptools.package-data]` entry (no per-variant TOML edit required).

Do **not** commit `skills/_assembled/` under a variant; that directory is
produced at deploy time by the skills brick.

### Maintaining `portfolio_assistant`

The tree mirrors the `instances/portfolio-assistant/` layout of the
`agent-foundry-sample-agent` downstream repo. That repo may be consumed either
by pinning the public upstream or by pinning your own fork -- both are valid
(see the [README adoption model](../README.md#how-organizations-use-this-repository)).
After syncing, run the init/renderer tests to confirm identity rewrite and
wheel packaging still pass.
