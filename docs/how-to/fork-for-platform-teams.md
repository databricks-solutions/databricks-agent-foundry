# Fork this repository for a platform team

> Configure GitHub Actions, reviewers, a package index, and a Databricks
> workspace so a **framework fork** can run the same CI and acceptance
> canaries as upstream. Application-repo pipelines (the files
> `agent-foundry init` writes) are covered in
> [Deploy via CI/CD](deploy-cicd.md).

This page is for platform / enablement teams. Application teams pin a SHA
of your fork and never need write access here.

## The two repositories

| Repository | Who owns it | What CI does |
|------------|-------------|--------------|
| **This fork** (framework) | Platform team | Lint, offline tests, packaged `init` smoke, then live brick / payload / example canaries against a dedicated workspace |
| **Generated app repo** | Product team | Lint + unit tests on PRs; approved staging then prod deploys |

`agent-foundry init` copies workflow **templates** from
`agent_foundry/init/ci_templates.py`. Changing those files in the fork
affects **new** app repos only.

```
feature/* ──PR──▶ main ──PR──▶ release ──announce SHA──▶ app teams pin it
                   │             │
                   └ ci.yml      └ ci.yml + acceptance.yml (canaries)
```

Point teams at **your** `ORG` / `REPO` / `SHA`, not at public `main`, unless
you intentionally track upstream.

## How framework CI works

Four workflows live under `.github/workflows/`. Composite actions live under
`.github/actions/`.

### Offline CI — `ci.yml`

Runs on every pull request, every push to `main`, and `workflow_dispatch`.
No Databricks credentials.

| Job | Proves |
|-----|--------|
| Static checks (ruff) | format + lint |
| Static checks (mypy) | types on the configured surface, then filtered gates on `agent_foundry/`, `tests/`, `bricks/` |
| Offline tests | `pytest -m "not live and not component"` plus Agent UI TypeScript tests |
| Packaged example smoke | wheel install, `init` of every shipped example, each generated instance's `tests/unit` |

Every job authenticates to the configured PyPI index, then installs with
hashed `uv pip sync` (see [Package index](#2-package-index--your-pypi-mirror)).

### Framework acceptance — `acceptance.yml`

Runs on pull requests **into `release`** and on `workflow_dispatch`. It is
the consumer journey at the candidate SHA:

```
catalog (scenario matrix)
  → serving live     (Model Serving from the runner; no App; report only)
  → brick canaries   (deploy --only each brick, live checks, destroy, gone checks)
  → payload canaries (deploy, wait, smoke; then tracing + eval, report only)
  → example canaries (deploy, wait, smoke)
```

The release gate for payloads and examples is the same three commands an
app team runs: `agent-foundry deploy`, `wait`, `smoke`. `smoke` calls the
deployed App over `/invocations`, MCP and A2A (whatever the payload
enables) and checks that each answers. Tracing, evaluation and Agent
serving live run with `continue-on-error`: read their results, but they do
not fail the run.

Those four live jobs start together after `catalog`. They use distinct
`af-<code>-canary` names, so they do not wait on each other. Inside the
brick job, scaffolds still deploy in order (the vector index needs the
data-domain table).

Default scenarios (override with the workflow_dispatch `scenarios` input or
`AGENT_FOUNDRY_ACCEPTANCE_SCENARIOS`): `bricks`, `agent_payload`,
`portfolio_assistant`, `rest_mcp_proxy`. `rest_mcp_payload` and
`unity_gateway_assistant` are opt-in.

Each canary job:

1. Waits on the GitHub Environment `framework-acceptance` (required
   reviewers; secrets and variables are injected only after approval).
2. Runs `.github/actions/canary-journey`: install, `uvx agent-foundry init`
   from `git+https://github.com/<this-repo>.git@<sha>`, scaffold. The brick
   canary also destroys the previous run under the same stable names.
3. Deploys to the `dev` target in the generated project (CI-only; app
   repos never deploy `dev`).
4. Leaves a payload / example canary up for probing. The next run updates
   `af-<code>-canary` in place; dispatch with `reset` to destroy it first
   when the deployment has drifted.

Concurrency group `framework-acceptance-${{ github.workflow }}` with
`cancel-in-progress: false` — do not overlap two acceptance runs against
the same workspace names.

### Acceptance janitor — `acceptance-janitor.yml`

Daily (and `workflow_dispatch`). Report-only: lists leaked Apps. Uses the
same environment and index as acceptance. It does not delete resources.

### Dependabot

`.github/dependabot.yml` opens weekly grouped GitHub Actions updates only.
Python dependencies get alerts but no Dependabot PRs; see
[Dependabot](#6-dependabot) in the checklist.

### Composite actions you will likely edit

| Action | What it hard-codes today |
|--------|--------------------------|
| `package-index-auth` | GitHub OIDC audience `package-index-github`; the package index grant `provider_name: github-actions`; `~/.netrc` machine `pypi.org` |
| `uv-pip-sync` | Hashed export + `uv pip sync --require-hashes` against the index you pass in |
| `canary-journey` | Expects `DATABRICKS_HOST` + `DATABRICKS_CLIENT_ID`; pins the framework as a git dependency using `github.token` |

---

## Fork checklist

Work top to bottom. Until the runner, index, and environment exist, every
workflow will fail closed.

### 1. Runners

All three workflows set:

```yaml
runs-on:
  group: ubuntu-latest
  labels: linux-ubuntu-latest-2core-8gb
```

Replace `group` and `labels` with a runner group your org can use (or
`runs-on: ubuntu-latest` if GitHub-hosted runners are allowed). Change it
in every workflow file in `.github/workflows/`. Generated app-repo
templates already use `ubuntu-latest`; change
`agent_foundry/init/ci_templates.py` if app teams must use the same group.

Jobs need:

- Python 3.11 and Node 24 (offline CI's Agent UI tests)
- Outbound HTTPS to GitHub, your PyPI index, and (for canaries) the
  Databricks workspace
- `permissions: id-token: write` on jobs that federate to the package index or Databricks

### 2. Package index / your PyPI mirror

Upstream CI cannot reach public `pypi.org`. Installs go through the package index
(`PyPI`) via GitHub OIDC. Forks almost always need to retarget this.

Two places currently name Databricks's index:

| Location | What to change |
|----------|----------------|
| `env.INDEX_OIDC_URL` and `env.PYPI_INDEX` in `ci.yml`, `acceptance.yml`, `acceptance-janitor.yml` | Your token-exchange URL and simple index |
| `.github/actions/package-index-auth/action.yml` | `machine` in `~/.netrc`, OIDC `audience`, and `provider_name` must match **your** registry's GitHub OIDC app |

`uv.lock` records the index each package was fetched from (hundreds of
`registry = "https://pypi.org/simple"` lines). After
you point uv at your mirror, regenerate the lock (`uv lock`) so CI
agrees. Hashed installs (`--require-hashes`) need
**byte-identical** artifacts; a mirror that rebuilds wheels will fail
`uv pip sync`.

Pick one variant:

**A. Your own the package index (or compatible OIDC registry)**

1. Register GitHub Actions as an OIDC identity provider on the registry
   (issuer `https://token.actions.githubusercontent.com`).
2. Point `INDEX_OIDC_URL` / `PYPI_INDEX` at your host.
3. Edit `package-index-auth` so `~/.netrc` uses your hostname and the
   audience / `provider_name` your registry expects.
4. Keep hashed `uv-pip-sync` as-is.

**B. Another mirror (Nexus, CodeArtifact, Artifactory without this OIDC shape)**

Replace `package-index-auth` with whatever obtains a token (OIDC, or a
secret stored **only** on the `framework-acceptance` environment / org
secret store — not repo-wide). Keep writing credentials to `~/.netrc`
rather than embedding them in the index URL. `uv-pip-sync` can
stay; it only needs `UV_INDEX_URL` / `--default-index` and `~/.netrc`.

**C. Direct `pypi.org`**

1. Delete the the package index auth step from every job (or make it a no-op).
2. Point `uv pip sync` at `https://pypi.org/simple` (or omit
   `--default-index`).

Local developer machines need the same index (uv/pip config). Otherwise
`uv lock` on a laptop will rewrite `uv.lock` back to public PyPI.

The Databricks **App container** installs the generated `requirements.txt`
inside the workspace, not on the GitHub runner. If the workspace blocks
public PyPI, confirm with your workspace admin how Apps resolve packages
before you rely on canaries or team deploys.

### 3. GitHub Environment `framework-acceptance`

Do not put Databricks credentials in repository-level Actions secrets.
Canary jobs read them from this environment, and they are only available
after the required reviewers approve the deployment.

**Settings → Environments → New environment** named exactly
`framework-acceptance` (the workflow `environment:` key).

| Setting | Recommendation |
|---------|----------------|
| Required reviewers | List the people or team who may approve live deploys. GitHub lets **any one** listed reviewer approve the job |
| Prevent self-review | Enable if you want the person who opened the PR to be unable to approve their own canary |
| Deployment branches | Optionally limit to `release` and the default branch so a random feature branch cannot dispatch into the workspace |
| Wait timer | Optional extra delay; not a substitute for a second human |

GitHub's environment gate does not natively require *two* approvals on
one job. To get two humans in the loop, combine this environment with
branch-protection "require 2 approving reviews" on PRs into `release`,
or chain two environments (for example `framework-acceptance` then
`framework-acceptance-deploy`) and list different reviewers on each.

**Environment secrets** (Settings → Environments → `framework-acceptance`
→ Environment secrets):

| Secret | Required | Purpose |
|--------|----------|---------|
| `DATABRICKS_HOST` | yes | Workspace URL, no trailing slash |
| `DATABRICKS_CLIENT_ID` | yes | Service principal application / UUID |
| `DATABRICKS_CLIENT_SECRET` | no | OAuth M2M. If **unset/empty**, jobs set `DATABRICKS_AUTH_TYPE=github-oidc` |

**Environment variables** (non-secret):

| Variable | Required | Purpose |
|----------|----------|---------|
| `AGENT_FOUNDRY_TEST_CATALOG` | yes | Unity Catalog the canaries write into. Hosts may be shared; catalogs must be unique per environment |
| `AGENT_FOUNDRY_TEST_VS_ENDPOINT` | brick canary | Existing Vector Search endpoint. Destroy removes only the index, not the endpoint |

OIDC (no stored client secret):

1. An account admin creates a federation policy on the service principal.
   Subject must match the environment:

   ```text
   repo:<org>/<fork>:environment:framework-acceptance
   ```

   Audience is the workspace token endpoint
   `https://<workspace-host>/oidc/v1/token`. Full command:
   [Deploy via CI/CD — GitHub OIDC](deploy-cicd.md#github-without-a-stored-secret-oidc-federation).

2. Keep `permissions: id-token: write` (already on the workflows).

3. Leave `DATABRICKS_CLIENT_SECRET` unset so the
   `secrets.DATABRICKS_CLIENT_SECRET == '' && 'github-oidc'` expression
   selects federation.

`canary-journey` fails closed if `DATABRICKS_HOST` or
`DATABRICKS_CLIENT_ID` is empty after approval.

### 4. Workspace prerequisites

Create a dedicated catalog (the value of `AGENT_FOUNDRY_TEST_CATALOG`)
rather than sharing a production catalog. The canary service principal
needs enough privilege to create and destroy the resources the scenarios
exercise:

| Capability | Used by |
|------------|---------|
| Create schemas, volumes, tables, UC functions in the test catalog | data, sql_function, tracing, evaluation |
| Create and manage SQL warehouses | warehouse brick; payload `requires: [warehouse, …]` |
| Create Databricks Apps, jobs, Genie spaces, Vector Search indexes, Lakebase, MCP | brick / payload / example canaries |
| Create MLflow experiments and UC trace destinations | tracing |
| Call the model serving endpoints named in scaffolds | live agent checks |

Two workspace facts that are easy to miss:

**`account users` needs `USE CATALOG` on the test catalog.** App UC table
resources (`uc_securable`) are rejected when account users lack
`USE CATALOG` and the deploying principal lacks `MANAGE` on the catalog:

```sql
GRANT USE CATALOG ON CATALOG <test_catalog> TO `account users`;
```

Grant this as a catalog admin; the canary SP often cannot.

**Model serving.** The agent scaffold defaults to
`databricks-claude-sonnet-4-5`. The report-only reasoning checks and the
portfolio example use `databricks-claude-sonnet-5-5` (see
`FOUNDRY_REASONING_E2E_ENDPOINT` in `acceptance.yml`). Provision those
endpoints, or change the scaffold / workflow to names you serve.

**Vector Search.** Create one endpoint and set
`vars.AGENT_FOUNDRY_TEST_VS_ENDPOINT` to its name. The brick canary
fails closed if the variable is empty when it deploys `vector_indexes`.
Endpoints are shared workspace infrastructure; canaries isolate by
instance `schema_prefix` (the index's catalog.schema.name), not by
creating a private endpoint.

Canary identity is `AGENT_FOUNDRY_RUN_ID=canary`, which yields stable
names (`af-<code>-canary`). Do not point a second fork at the same
workspace + catalog without changing the run id, or the two will destroy
each other's Apps.

### 5. CODEOWNERS, reviewers, branch protection

`.github/CODEOWNERS` currently requires named upstream maintainers on
`.github/`. Replace those handles with your platform team so unsigned
workflow edits cannot merge without you.

Recommended branch rules (Settings → Branches), applied to `main` and
`release`:

- Require a pull request; dismiss stale reviews
- Require a number of approving reviews that matches your change-control
  bar (use 2 if environment approval is only one person)
- Require the `ci.yml` checks: `Static checks (ruff)`,
  `Static checks (mypy)`, `Offline tests`, `Packaged example smoke`
- On `release`, do not require the canary jobs as status checks — they
  run *after* environment approval on the PR, not as classic
  required-status names you can tick in the same way. Gate them with
  the environment instead
- Restrict who can push to `release`

PRs into `release` are how you cut a candidate. App teams should pin a
SHA that has a green acceptance run.

### 6. Dependabot

1. Confirm the GitHub App can open GitHub Actions update PRs against the
   fork.
2. Keep Dependabot alerts on, but turn off **Dependabot security updates**
   (Settings → Code security). Its Python PRs bump one package without its
   exact-pinned siblings or the lock-derived App constraints. Fix alerts
   with an override in `pyproject.toml`, `uv lock`, and
   `python -m agent_foundry.constraints.export`.

### 7. Generated app-repo templates

Edit `agent_foundry/init/ci_templates.py` when app teams should inherit
your defaults:

| Upstream default | Typical fork change |
|------------------|---------------------|
| `runs-on: ubuntu-latest` | Your runner group |
| `FRAMEWORK_REPO_TOKEN` + `git config url.insteadOf` rewriting `git@github.com:` | Required if the fork is private. Issue a **read-only** PAT (or GitHub App installation token) scoped to the framework repo; app teams store it as a repo or org secret. Public forks can drop the rewrite |
| `uv sync --frozen` | Resolves from whatever indexes are recorded in **that app repo's** `uv.lock`. After you re-lock the framework against your mirror, new `init`s pick it up |
| GitHub Environments `staging` and `prod` | Create those environments on every app repo (or document the click-ops). Put `DATABRICKS_HOST_*` / `CLIENT_ID_*` / `CLIENT_SECRET_*` there, with required reviewers. Details: [Deploy via CI/CD](deploy-cicd.md) |

`init` pins `agent-foundry @ git+ssh://git@github.com/ORG/REPO.git@SHA`.
Tell teams the `ORG` / `REPO` / `SHA` to use; the README wizard block is
the copy they follow.

---

## Cutting a release

1. Land work on `main` (offline CI only).
2. Open a PR `main` → `release`. That triggers `acceptance.yml`.
3. For each canary job, a required reviewer approves the
   `framework-acceptance` deployment.
4. Brick canaries destroy themselves after checks. Payload and example
   canaries stay up; poke the App if you want, then merge.
5. Announce the merge SHA. Application teams bump the pin:

   ```bash
   uv lock --upgrade-package agent-foundry
   uv sync --frozen
   ```

Manual `workflow_dispatch` on "Framework acceptance" re-runs the same
path (optionally with a subset of scenario ids, or `reset` to rebuild the
payload / example canaries) without a release PR.

Do not overlap two acceptance runs; a second run against the same names
will fight the first.

---

## Troubleshooting

| Symptom | What to check |
|---------|----------------|
| Every job: runner not found | `runs-on.group` / `labels` still name the upstream EMU group |
| `Failed to obtain the package index access token` or `Failed to obtain GitHub OIDC id token` | OIDC app on the registry; audience / `provider_name`; `id-token: write`; hostname in `~/.netrc` |
| `uv pip sync` hash mismatch | Mirror is not serving the same bytes as the lock; re-lock from that index or fix the mirror |
| `DATABRICKS_HOST / DATABRICKS_CLIENT_ID are empty` | Secrets live on the **environment**, not the repo; the job has not been approved yet, or the names differ |
| `invalid_client` / OIDC subject mismatch | Federation policy subject is not `repo:<org>/<fork>:environment:framework-acceptance` |
| Vector canary: `vars.AGENT_FOUNDRY_TEST_VS_ENDPOINT is not set` | Environment **variable** (not secret) on `framework-acceptance` |
| App deploy warning: account users lack `USE CATALOG` | Grant on the test catalog (see [Workspace prerequisites](#4-workspace-prerequisites)) |
| Live agent / reasoning tests fail with endpoint 404 | Serving endpoints `databricks-claude-sonnet-4-5` and `databricks-claude-sonnet-5-5` (or change the names) |
| Two forks clobber the same Apps | Same catalog + `AGENT_FOUNDRY_RUN_ID=canary`; give the second fork its own catalog or run id |
| Dependabot "Cannot read TLS response" | Still pointed at an unreachable index; see variant C under [Package index](#2-package-index--your-pypi-mirror) |

Reproduce a canary on a laptop (your profile, your own `--run-id`) with
[tests/README.md — Reproduce a canary locally](../../tests/README.md#reproduce-a-canary-locally).

---

## Related pages

- [Deploy via CI/CD](deploy-cicd.md) — generated **application** repos
- [Getting started](../tutorials/getting-started.md) — init → first deploy
- [tests/README.md](../../tests/README.md) — what each CI job proves
- [Environment variables](../reference/env-vars.md) — `AGENT_FOUNDRY_*` and Databricks auth vars
- [README — How organizations use this repository](../../README.md#how-organizations-use-this-repository)
