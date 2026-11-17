# CLI Reference

Every developer workflow goes through the `agent-foundry` typer CLI installed by `[project.scripts]`. Run `uv run agent-foundry --help` for the live list, or `uv run agent-foundry <verb> --help` for verb-specific options.

## Verbs

| Verb | Purpose |
|------|---------|
| `agent-foundry init` | Bootstrap a new downstream agent application repo (interactive wizard). |
| `agent-foundry scaffold <brick>` | Write a fully-commented starter config for a brick. |
| `agent-foundry deploy` | Deploy one or all instances (all bricks in dependency order). |
| `agent-foundry destroy` | Tear down deployed resources (per-brick or full). |
| `agent-foundry wait` | Block until the deployed App is running; prints its URL (use before acceptance tests). |
| `agent-foundry smoke` | Probe the deployed App through its public protocols; exits non-zero on any failure. |
| `agent-foundry validate` | Local + workspace prerequisite checks. |
| `agent-foundry run` | Start the local AgentServer + chat UI. |
| `agent-foundry repl` | Interactive agent REPL. |
| `agent-foundry test` | pytest passthrough (unit / component / live). |
| `agent-foundry eval` | MLflow GenAI evaluation (direct or trace mode). |
| `agent-foundry monitors {start,stop,status,delete}` | Production monitoring scorers. |
| `agent-foundry call {a2a,mcp}` | Call a deployed agent (needs the `[call]` extra). |
| `agent-foundry genie import` | Import a live Genie Space into the repo. |
| `agent-foundry config show` | Inspect the typed `foundry.toml` config for a target/instance. |
| `agent-foundry sync-schemas` | Copy the wheel's JSON Schemas into `.agent-foundry/schemas/` for IDE validation. |
| `agent-foundry migrate quote-identifiers` | Back-quote catalog/schema identifiers in the project's own `.sql` files. |
| `agent-foundry brick <name> <action>` | Escape hatch into any individual brick action. |

## Project setup

```bash
# Bootstrap a brand-new application repo (interactive wizard). Run via uvx so
# you don't need the framework installed first -- substitute ORG/REPO/SHA.
uvx --from "agent-foundry @ git+ssh://git@github.com/ORG/REPO.git@SHA" \
    agent-foundry init

# Name the project up front (the wizard still prompts for targets, CI, template)
uv run agent-foundry init --name my-agent --outdir ~/projects

# Add a brick's fully-commented starter config to an existing instance
uv run agent-foundry scaffold --list                          # list available bricks
uv run agent-foundry scaffold genie --instance <your-app>
uv run agent-foundry scaffold warehouse --instance <your-app> --force   # overwrite existing
```

## Configuration & schemas

```bash
# Print the fully-resolved typed foundry.toml config for a target/instance
uv run agent-foundry config show --target dev --instance <your-app>
uv run agent-foundry config show --target dev --instance <your-app> --format json
uv run agent-foundry config show --target dev --instance <your-app> -f yaml

# Copy the wheel's JSON Schemas into the repo for IDE validation/autocomplete
uv run agent-foundry sync-schemas                             # -> .agent-foundry/schemas/
uv run agent-foundry sync-schemas --repo-root path/to/repo --force
```

`config show` resolves `--instance` automatically when the repo declares a single instance; pass it explicitly when there is more than one. Re-run `sync-schemas` after every framework upgrade so the on-disk schemas track the installed wheel.

## Upgrading an existing project

Some fixes land in the shipped templates under `bricks/**/_scaffold`, which a project
copied when it was scaffolded. Upgrading the wheel cannot reach those copies, so the
`migrate` verbs rewrite them in place:

```bash
# Preview first -- prints every line it would change, writes nothing
uv run agent-foundry migrate quote-identifiers --dry-run

# Apply, optionally scoped to one instance
uv run agent-foundry migrate quote-identifiers
uv run agent-foundry migrate quote-identifiers --instance <your-app>
```

`quote-identifiers` back-quotes `${var.catalog}` and `${var.schema}` in identifier
positions and replaces the two-part `${var.agent_schema}` with
`` `${var.catalog}`.`${var.agent_schema_name}` ``. Unity Catalog allows a hyphen in a
catalog name where Spark SQL requires a back-quoted identifier, which is why an
unquoted template deploys cleanly in one workspace and fails with
`INVALID_IDENTIFIER` in another. Volume paths and comments are left alone, and the
command is safe to re-run: an already-quoted file is reported and skipped.

## Deployment

```bash
# Full deploy (all bricks, one instance)
uv run agent-foundry deploy --target dev --instance <your-app>

# Dry-run: assemble, validate, and preview without mutating.
# Still reads live workspace state (Genie/MCP destroy plans, prior outputs).
uv run agent-foundry deploy --target dev --instance <your-app> --dry-run

# Hermetic plan: never construct a WorkspaceClient (CI / unit / no credentials).
uv run agent-foundry deploy --target dev --instance <your-app> --dry-run --offline

# Deploy + run integration tests after each brick
uv run agent-foundry deploy --target dev --instance <your-app> --run-tests

# Deploy all discovered instances (any dir with foundry.toml)
uv run agent-foundry deploy --target dev

# Skip specific bricks (comma-separated)
uv run agent-foundry deploy --target dev --instance <your-app> --skip warehouse,observability

# Deploy only specific bricks (comma-separated, substring match like --skip)
uv run agent-foundry deploy --target dev --instance <your-app> --only warehouse,skills

# Override variables from the command line (repeat --vars per pair)
uv run agent-foundry deploy --target dev --instance <your-app> \
    --vars catalog=my_catalog --vars warehouse_id=abc123

# Tear down everything in reverse deploy order (safe by default)
uv run agent-foundry destroy --target dev --instance <your-app>

# Preview teardown (may read live workspace state)
uv run agent-foundry destroy --target dev --instance <your-app> --dry-run

# Hermetic destroy plan (no WorkspaceClient)
uv run agent-foundry destroy --target dev --instance <your-app> --dry-run --offline

# Include destructive resources (UC schemas, SQL functions, Lakebase, trace tables)
uv run agent-foundry destroy --target dev --instance <your-app> --force

# Destroy a single brick (accepts deploy-registry or CLI name, e.g. genie or genie_spaces)
uv run agent-foundry destroy --target dev --instance <your-app> --brick data_domains
```

`--dry-run` is **not** offline: it still constructs a workspace client when credentials exist so preview can load workspace-only state. `--offline` requires `--dry-run` and never constructs a client. Unit tests and packaging smokes use `--offline`.

`--skip` and `--only` are mutually exclusive; passing both is an error. `--only` matching is substring-based against each brick's display name, registry name, and CLI alias, so a short token like `--only vector` may match more than one brick -- use a more specific token to narrow. A `--only` token that matches no brick is an error (it would otherwise silently deploy nothing). Because `--only` deploys a subset, deploying a downstream brick alone (e.g. `--only app`) relies on upstream brick outputs from a prior deploy; on a fresh instance the upstream bricks must be deployed first.

### Smoke-testing a deployment

```bash
uv run agent-foundry wait  --instance <your-app> --target dev
uv run agent-foundry smoke --instance <your-app> --target dev
```

`smoke` asks the instance's payload adapter for black-box probes and runs every one, printing `PASS`/`FAIL` per probe. The `agent` payload calls `/invocations` and, when enabled in the agent spec, the MCP `ask_agent` tool and the A2A agent card plus `SendMessage`. The `rest_mcp` payload lists tools through the UC MCP gateway. An optional `smoke.yml` next to the instance's `foundry.toml` adds questions sent to `/invocations`:

```yaml
questions:
  - What is Unity Catalog?
  - question: Triage ticket 42 using the triage skill.
    expect_contains: [TRIAGE-OK]
```

Probes check that a protocol answers with non-empty output. A question's optional `expect_contains` also requires each listed substring (case-sensitive) in the `/invocations` answer, for deterministic markers such as one a skill instructs the agent to emit. Answer quality is the job of `agent-foundry eval`.

`destroy` requires `--instance` (unlike `deploy`, it does not support all-instances mode). It walks every registered brick via `destroy_instance()` in reverse deploy order. Each brick reports destructive items through `DestroyPlanItem.destructive`; those are skipped unless `--force` is set (or you confirm skipping them on a TTY).

## Local Development

```bash
# Start backend + chat UI for an instance
uv run agent-foundry run --instance <your-app>
uv run agent-foundry run --instance <your-app> --port 8001 --no-ui

# Interactive REPL (no UI, fastest feedback loop)
uv run agent-foundry repl --instance <your-app>

# Power users: invoke a brick script directly via the dispatch table
uv run agent-foundry brick app start-server --instance <your-app> --reload
```

## Individual Brick Operations

The `brick` verb is an escape hatch into the per-brick scripts. The first argument is the brick name (`warehouse`, `data`, `genie`, `vector_search`, `job`, `skills`, `mcp`, `tracing`, `app`, `observability`, `evaluation`, `sql_function`); the second is one of its registered actions (run `uv run agent-foundry brick <name> --help` for the list, or peek at [`agent_foundry/deploy/_brick_registry.py`](../../agent_foundry/deploy/_brick_registry.py)).  The deprecated `agent` and `agent_app` CLI aliases were removed in agent-foundry 2.0 (use `tracing` and `app`).  Lakebase is no longer a separate brick -- chat history is provisioned by `app deploy` when `spec.ui.chat_history.enabled = true` (the default) in `payloads/agent/payload.yml`.

Like `deploy --only`, a brick invoked this way relies on upstream outputs from a prior deploy, and it reads them from the same recorded state: `warehouse_id`, `genie_space_id__<space>`, `vs_index_name__<index>` and friends are loaded from the workspace state under `/Shared/agent-foundry/<instance>/_state/` and need no flag. Deploy the upstream brick first on a fresh instance. Passing `--vars warehouse_id=abc123` overrides the recorded value when you want to point a single brick somewhere else.

```bash
# Warehouse
uv run agent-foundry brick warehouse deploy --instance <your-app> --target dev
uv run agent-foundry brick warehouse deploy --instance <your-app> --action destroy

# Data Domains (app name for all domains, or <app>/<domain> for one)
uv run agent-foundry brick data deploy --instance <your-app> --target dev
uv run agent-foundry brick data deploy --instance <your-app>/docs_corpus --target dev

# Genie Spaces
uv run agent-foundry brick genie assemble --instance <your-app>/info_schema
uv run agent-foundry brick genie validate --instance <your-app>
uv run agent-foundry brick genie deploy   --instance <your-app> --target dev
uv run agent-foundry genie import --space-id 01f0abcd1234 --instance <your-app>/info_schema

# Vector Search
uv run agent-foundry brick vector_search deploy-index   --instance <your-app> --target dev
uv run agent-foundry brick vector_search validate-index --instance <your-app>/docs_search --target dev
uv run agent-foundry brick vector_search export-index   --index-name catalog.schema.docs_index --output exported/

# Jobs
uv run agent-foundry brick job validate --instance <your-app>/jobs/<job> --target dev
uv run agent-foundry brick job deploy   --instance <your-app>/jobs/<job> --target dev

# MCP Servers
uv run agent-foundry brick mcp deploy   --instance <your-app> --target dev
uv run agent-foundry brick mcp deploy   --instance <your-app> --target dev --dry-run
uv run agent-foundry brick mcp inspect  <server-name> --instance <your-app> --target dev   # needs [call] extra
uv run agent-foundry brick mcp resolve  --instance <your-app> --target dev [--as sp | --as-current-user]   # tools.mcp.gateway
uv run agent-foundry brick mcp validate instances/<your-app>/payloads/agent/mcp_servers/<server-name>/server.yml

# Skills, MLflow, Agent App, Dashboard
uv run agent-foundry brick skills assemble            --instance <your-app> --target dev
uv run agent-foundry brick tracing ensure-experiment  --instance <your-app> --target dev
uv run agent-foundry brick app deploy                 --instance <your-app> --target dev
uv run agent-foundry brick observability deploy-dashboard --instance <your-app> --target dev
```

## Evaluation & Monitoring

```bash
uv run agent-foundry eval --instance <your-app> --target dev --mode direct
uv run agent-foundry eval --instance <your-app> --target dev --mode traces
uv run agent-foundry eval --instance <your-app> --target dev \
    --mode traces --dataset catalog.schema.golden_set --max-traces 50

uv run agent-foundry monitors start  --instance <your-app> --target dev
uv run agent-foundry monitors status --instance <your-app> --target dev
uv run agent-foundry monitors stop   --instance <your-app> --target dev
uv run agent-foundry monitors delete --instance <your-app> --target dev
```

Both commands read the scorer lists from `instances/<your-app>/evaluation/eval_config.yml`. To add your own scorers, put `@scorer` functions in `instances/<your-app>/evaluation/scorers/*.py` and list them by name; see [Instance scorers](../../bricks/evaluation/README.md#instance-scorers). `eval --dry-run` prints the resolved scorer list.

## Calling a deployed agent

Install the optional `[call]` extra once:

```bash
uv pip install 'agent-foundry[call]'
```

The friendly path uses `--instance` (with `--target`, default `dev`). The CLI loads the instance config, resolves the app's `app_resource_name`, and looks up the deployed hostname via `WorkspaceClient.apps.get(...).url` -- you don't need to copy the random Databricks Apps host yourself.

```bash
# MCP (single-turn ``ask_agent``); same shape as agent-foundry repl --instance ...
uv run agent-foundry call mcp --instance <your-app> 'what is the answer?'
uv run agent-foundry call mcp --instance <your-app> --target staging 'what is the answer?'

# A2A (streaming, multi-turn capable)
uv run agent-foundry call a2a --instance <your-app> 'what is the answer?'
```

The `MESSAGE` argument is optional. If you omit it the CLI reads from stdin when piped, otherwise it prompts interactively -- so all three of these work:

```bash
uv run agent-foundry call mcp --instance <your-app> 'what is the answer?'   # positional
echo 'what is the answer?' | uv run agent-foundry call mcp --instance <your-app>   # stdin
uv run agent-foundry call mcp --instance <your-app>                          # interactive prompt
```

`--profile` overrides the target's configured profile if you need to authenticate as a different identity. Pass `--app-host` directly when you want to bypass instance loading entirely (e.g. raw API testing or hitting an external deployment):

```bash
uv run agent-foundry call mcp --app-host my-agent-1234.cloud.databricksapps.com \
    --profile demo 'what is the answer?'
```

If both `--instance` and `--app-host` are supplied, `--app-host` wins (explicit override) and a one-line stderr note flags that `--instance`/`--target` were ignored.

## Validation & Testing

```bash
# Offline prerequisite checks (Python, modules, project layout, framework, instances)
uv run agent-foundry validate

# Include workspace connectivity checks (auth, warehouse, UC)
uv run agent-foundry validate --workspace --target dev

# Run the test suite (pytest passthrough)
uv run agent-foundry test                       # unit (plugin skips other markers)
uv run agent-foundry test --component           # -m component --run-component
uv run agent-foundry test --live                # -m live --run-live
```

### Pytest markers

Each owner (`agent_foundry/`, `bricks/*/`, `payloads/*/`, `examples/*/`, and
every generated `instances/<name>/`) keeps offline
tests in its own `tests/unit/` and checks against a CLI deployment in a
real workspace in its own `tests/live/`. Repo-wide rules live under
`tests/contracts` and packaging smokes under `tests/component`. The pytest
plugin registers the markers and marks every test under `tests/live/` as
`live`; markers remain the CI filter.

| Marker | Meaning | How to run |
|--------|---------|------------|
| *(unmarked)* | Unit: in-process, mocks/fakes, no credentials, no fresh venv. Includes Typer `CliRunner` and the cheap `agent-foundry deploy --dry-run --offline` console-script smoke. | `pytest -m "not live and not component"` |
| `component` | Packaging / packaged CLI: wheel + fresh venv, no Databricks. | `pytest -m component --run-component` |
| `live` | Automatic for `tests/live/`: verifies a CLI deployment in a real workspace (bricks, payloads, deployed Apps). | `pytest -m live --run-live` |
| `gone` | Inside `tests/live/`: after `destroy`, the deployment left nothing behind. | `pytest -m gone --run-live --run-gone` |

Live tests get the deployment through the `live_target`, `live_ws` and
`live_variables` fixtures. They skip when the workspace binding or the
single `instances/<name>/` is missing; with `AGENT_FOUNDRY_REQUIRE_LIVE=1`
(canary jobs) they fail instead. `destroy` deletes the state the outputs
come from, so `live_variables` records them in the pytest cache and `gone`
checks read them back through `gone_variables`: run the live checks before
destroy, from the same checkout.

`--dry-run` remains workspace-aware (read-only live state). `--offline` is the
hermetic CLI path and never constructs a `WorkspaceClient`. The skip-env
tripwire (`AGENT_FOUNDRY_SKIP_DEFAULT_WORKSPACE_CLIENT`, set by the pytest
plugin and the unit CI job) is a fail-closed guard, not an offline signal.
Unit tests inject `ws=None` or a fake; they must not `delenv` that flag
except in tests of the factory itself. Packaging smokes pop the skip-env
and pass `--offline`.

## Power users / debugging

The `agent-foundry` CLI is the supported public surface. Underneath it, every brick action is still backed by a plain `bricks/<name>/scripts/<script>.py` module that exposes the same `main(argv)` entry point and accepts the same flags. The CLI just resolves them through [`agent_foundry/deploy/_brick_registry.py`](../../agent_foundry/deploy/_brick_registry.py).

For one-off debugging, profiling, attaching a debugger, or running under a non-default interpreter, you can invoke a brick script directly:

```bash
# Equivalent to: uv run agent-foundry brick warehouse deploy ...
python bricks/warehouse/scripts/deploy_warehouse.py --instance <your-app> --target dev

# Equivalent to: uv run agent-foundry run --instance <your-app> --port 8001
python payloads/agent/runtime/start_server.py    --instance <your-app> --port 8001
```

Direct invocation is supported but unadvertised -- prefer `agent-foundry brick <name> <action>` for everyday work and reach for the script path only when the CLI gets in your way (e.g. running under `python -m pdb`, `python -X importtime`, or a profiler).
