# Getting Started

> From zero to a deployed agent on Databricks.

Agent Foundry is consumed as a **library**: you generate your own application
repo that *pins* the framework, instead of cloning or forking the framework
and editing it in place. This tutorial walks the full path -- bootstrap,
configure, deploy -- using the `agent-foundry init` wizard.

## Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)
- The [Databricks CLI](https://docs.databricks.com/dev-tools/cli/install.html), authenticated:
  ```bash
  databricks auth login --host https://your-workspace.cloud.databricks.com
  ```
- A Databricks workspace with Unity Catalog and a serverless SQL warehouse.

## Step 1: Bootstrap a new agent repo

First decide where the framework lives -- pin to the public upstream, or pin
to your own fork. The [README adoption model](../../README.md#how-organizations-use-this-repository)
explains fork vs. upstream; in short, set `SHA` to the framework commit you
want to pin against.

Run the wizard with `uvx` (substituting `ORG`, `REPO`, `SHA`):

```bash
uvx --from "agent-foundry @ git+ssh://git@github.com/ORG/REPO.git@SHA" \
    agent-foundry init
```

The wizard collects:

- A **project name** (also the generated directory name).
- **Workspace targets** (`dev` required, `staging` / `prod` optional). Hosts
  may repeat across targets, but **catalogs must be unique** per target.
- A **CI provider** (`git` = GitHub Actions, `gitlab` = GitLab CI).
- A **starter template** -- `blank` (empty instance) or a named example such
  as `portfolio_assistant` (the full reference agent: Genie, Vector Search,
  SQL Functions, data layer, tracing, observability, evaluation).

For repeated bootstrapping, install the CLI once instead:

```bash
uv tool install "agent-foundry @ git+ssh://git@github.com/ORG/REPO.git@SHA"
agent-foundry init
```

## Step 2: What the wizard generated

`init` writes a self-contained application repo at `./<project_name>/`:

```
<project_name>/
├── foundry.toml                 # repo-root: workspace targets + instance list
├── instances/
│   └── <project_name>/          # your application -- bricks live here
│       ├── foundry.toml         # instance identity + cross-cutting infra
│       ├── app/config.yml       # generic host: compute, OBO, payload ref
│       ├── payloads/agent/      # agent payload: payload.yml (the single config), skills/, mcp_servers/
│       └── ...                  # one directory per infra brick you add
├── .agent-foundry/schemas/      # JSON Schemas for IDE validation/autocomplete
├── pyproject.toml               # pins `agent-foundry @ <SHA>`
└── .github/ (or .gitlab-ci.yml) # the CI provider you chose
```

The two `foundry.toml` files are the typed config contract; the per-brick
YAMLs carry the `${var.*}` content. See the
[configuration reference](../reference/config.md) for the full shape.

## Step 3: Configure your workspace targets

Edit the **repo-root** `foundry.toml`. The `dev` target is laptop-only (never
touched by CI):

```toml
#:schema .agent-foundry/schemas/foundry-repo.schema.json
schema_version = 2

[targets.dev]
host    = "https://your-workspace.cloud.databricks.com"
catalog = "dev_catalog"
profile = "your-cli-profile"

[[instances]]
path = "instances/<project_name>"
```

Instance identity (`name`, `schema_prefix`, warehouse mode, chat history) lives
in the **per-instance** `instances/<project_name>/foundry.toml`. Inspect the
fully-resolved view any time with:

```bash
uv run agent-foundry config show --target dev --instance <project_name>
```

> Editor support: the `#:schema` / `# yaml-language-server:` headers point at
> `.agent-foundry/schemas/`. Re-run `uv run agent-foundry sync-schemas` after a
> framework upgrade to refresh them.

## Step 4: Validate and deploy

```bash
cd <project_name>
uv run agent-foundry validate --workspace --target dev   # check prerequisites
uv run agent-foundry deploy --target dev --dry-run        # assemble + validate, no writes
uv run agent-foundry deploy --target dev                  # deploy all present bricks
```

`agent-foundry init` already installed the pinned framework into `.venv`. `uv run` executes the CLI inside it without activating a venv. If you cloned this repo on a new machine, run `uv sync --frozen` first. The orchestrator auto-discovers your instance and deploys its bricks in dependency order.

Run the agent locally against the deployed resources:

```bash
uv run agent-foundry run --instance <project_name>   # backend + chat UI
uv run agent-foundry repl --instance <project_name>  # no UI, fastest loop
```

## Step 5: Add more bricks

```bash
uv run agent-foundry scaffold --list                       # what's available
uv run agent-foundry scaffold genie --instance <project_name>
```

`scaffold` writes a fully-commented starter config (with a `$schema` header)
into the instance directory. Customize it, then redeploy.

The agent reaches a Genie Space or Vector Search index through an MCP server:
add `payloads/agent/mcp_servers/<name>/server.yml` and list the tools in
`tools.allow`. See
[`payloads/agent/deploy/mcp/README.md`](../../payloads/agent/deploy/mcp/README.md).

## Step 6: Import an existing Genie Space (optional)

If you already built a Genie Space in the UI, decompose it into editable
YAML/Markdown:

```bash
uv run agent-foundry genie import --space-id <space_id> --instance <project_name>
```

## Step 7: CI/CD

The generated repo ships a pipeline for the CI provider you chose, with the
same three-stage flow:

| Trigger | Jobs |
|---|---|
| PR `feature/*` -> `main` | unit / offline tests |
| PR `main` -> `release` | unit tests, then approved staging deploy + post-deploy acceptance |
| Merge to `release` | approved production deploy |

Configure OAuth service-principal secrets per environment
(`DATABRICKS_HOST_STAGING` / `DATABRICKS_CLIENT_ID_STAGING` /
`DATABRICKS_CLIENT_SECRET_STAGING`, and the `_PROD` equivalents); the
Databricks SDK auto-detects them. See the
[README CI section](../../README.md#ci-deploys-generated-repos) for details.

## Next steps

- [Architecture](../explanation/architecture.md) -- how the bricks fit together.
- [CLI reference](../reference/cli.md) -- every verb with examples.
- [Configuration reference](../reference/config.md) -- the `foundry.toml` contract.
- [Reference instance](../../examples/portfolio_assistant/README.md) -- a full worked example.
- [Documentation map](../index.md) -- everything else.
- [Fork for platform teams](../how-to/fork-for-platform-teams.md) -- if you own the framework fork, not an app repo.
- [Upgrading the framework](../../README.md#upgrading-a-generated-repository) -- bumping the pinned SHA.
