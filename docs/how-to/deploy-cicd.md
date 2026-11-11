# Deploy via CI/CD

> Set up automated deploys for a generated application repo. `agent-foundry
> init` already writes the pipeline files for the CI provider you chose; this
> guide explains the flow and how to wire the secrets.

## The three-stage flow

Both providers implement the same shape, keyed off the branch:

| Trigger | Jobs | Workspace auth |
|---------|------|----------------|
| PR `feature/*` → `main` | lint + unit / offline tests | none |
| PR `main` → `release` | cheap checks, then protected **staging** approval → deploy → post-deploy acceptance | staging service principal (after approval) |
| Merge to `release` | protected **prod** approval → deploy to prod | prod service principal (after approval) |

The `dev` target is laptop-only — CI never touches it. Staging and production are persistent application environments; generated pipelines never run `destroy` after those deploys.

```
feature/* ──PR──▶ main ──PR──▶ release ──push──▶ prod (approval)
                   │             │
                   └ unit tests  └ unit, then approved staging deploy + acceptance
```

## Files the wizard generated

- **GitHub Actions** (`.github/workflows/`):
  - `pr-to-main.yml` — lint + unit tests on PRs into `main`.
  - `pr-to-release.yml` — unit tests, then a `staging` Environment gate, deploy, wait, `-m live --run-live`. Staging is kept.
  - `release-deploy-prod.yml` — prod deploy on push to `release`, gated by the `prod` Environment. Never destroyed.
- **GitLab CI** (`.gitlab-ci.yml`): `test` then manual `deploy-staging` + `acceptance-staging`; prod deploy is `when: manual`.

Each pipeline ultimately runs:

```bash
uv run agent-foundry deploy --instance <project_name> --target <staging|prod>
```

## Workspace auth: client secret or OIDC

CI deploys as a Databricks service principal, in one of two ways:

| | Client secret (OAuth M2M) | GitHub OIDC federation |
|---|---|---|
| Stored credential | `DATABRICKS_CLIENT_SECRET_*`, long-lived, rotate before it expires | none; each job exchanges a short-lived GitHub token |
| Usable from | anywhere the secret is copied to | only jobs whose OIDC subject matches the policy (repo + environment) |
| Setup | workspace admin creates the secret | an **account admin** creates a federation policy per service principal |
| Providers | GitHub and GitLab | GitHub (this guide) |

Generated pipelines use the client secret. Switch a GitHub job to OIDC as
described [below](#github-without-a-stored-secret-oidc-federation); keep the
client secret where you cannot get an account admin, or for short-lived
workspaces where re-creating the policy each time is not worth it. Either
way, keep the secrets on a protected environment with required reviewers.

## Secrets (OAuth service-principal M2M)

Per target, expose three secrets. The Databricks SDK auto-detects them — no auth code is generated.

| Secret | Used for |
|--------|----------|
| `DATABRICKS_HOST_STAGING` / `DATABRICKS_CLIENT_ID_STAGING` / `DATABRICKS_CLIENT_SECRET_STAGING` | staging deploy + post-deploy acceptance |
| `DATABRICKS_HOST_PROD` / `DATABRICKS_CLIENT_ID_PROD` / `DATABRICKS_CLIENT_SECRET_PROD` | prod deploy |
| `AGENT_FOUNDRY_TEST_CATALOG` / `TEST_WAREHOUSE_ID` | non-secret GitHub **variables** (or GitLab CI variables) naming the test catalog and an optional existing warehouse for live tests |
| `FRAMEWORK_REPO_TOKEN` | read-only PAT so `uv sync` can fetch the pinned `agent-foundry` dependency from its private repo |

### GitHub

1. **Settings → Secrets and variables → Actions**: add the staging/prod secrets plus `FRAMEWORK_REPO_TOKEN`.
2. **Settings → Environments**: create `staging` and `prod` with **required reviewers**. Live jobs do not receive workspace secrets until that approval.

### GitHub without a stored secret (OIDC federation)

Instead of a client secret, a GitHub job can exchange its OIDC token for a
Databricks token ([workload identity federation](https://docs.databricks.com/aws/en/dev-tools/auth/provider-github)).

1. An account admin creates a federation policy on the service principal
   whose subject is the environment the job runs in. The audience is the
   workspace token endpoint, which is what the Databricks SDK requests for a
   workspace host:

   ```bash
   databricks account service-principal-federation-policy create <sp-numeric-id> --json '{
     "oidc_policy": {
       "issuer": "https://token.actions.githubusercontent.com",
       "audiences": ["https://<workspace-host>/oidc/v1/token"],
       "subject": "repo:<org>/<repo>:environment:staging"
     }
   }'
   ```

   The subject assumes GitHub's default claim format; if your org customizes
   it, match the custom format instead.

2. The job requests the token and names the auth type:

   ```yaml
   permissions:
     id-token: write
   env:
     DATABRICKS_AUTH_TYPE: github-oidc
     DATABRICKS_HOST: ${{ secrets.DATABRICKS_HOST_STAGING }}
     DATABRICKS_CLIENT_ID: ${{ secrets.DATABRICKS_CLIENT_ID_STAGING }}  # service principal UUID
   ```

#### Support both, switch by deleting the secret

To move to OIDC without a cut-over, let the job use the client secret while
it exists and OIDC once it is gone. An empty `DATABRICKS_AUTH_TYPE` counts as
unset, so the SDK falls back to OAuth M2M:

```yaml
permissions:
  id-token: write
env:
  DATABRICKS_AUTH_TYPE: ${{ secrets.DATABRICKS_CLIENT_SECRET_STAGING == '' && 'github-oidc' || '' }}
  DATABRICKS_HOST: ${{ secrets.DATABRICKS_HOST_STAGING }}
  DATABRICKS_CLIENT_ID: ${{ secrets.DATABRICKS_CLIENT_ID_STAGING }}
  DATABRICKS_CLIENT_SECRET: ${{ secrets.DATABRICKS_CLIENT_SECRET_STAGING }}
```

Create the federation policy, delete `DATABRICKS_CLIENT_SECRET_STAGING`,
and the next run authenticates through OIDC. The framework's own canaries
use this pattern.

### GitLab

1. **Settings → CI/CD → Variables** (mark each **masked**): add the same staging/prod secrets plus `FRAMEWORK_REPO_TOKEN`.
2. The prod deploy is already `when: manual` on the `release` branch — run it from the pipeline view when ready.

## Run the same checks locally before pushing

The pipeline jobs mirror commands you can run yourself:

```bash
uv run ruff check .
uv run pytest -q -m "not live and not component"
uv run agent-foundry deploy --instance <project_name> --target staging --dry-run
# After a real deploy (needs workspace auth):
uv run agent-foundry wait --instance <project_name> --target staging
uv run agent-foundry smoke --instance <project_name> --target staging
uv run pytest -q -m live --run-live
```

## Customizing

- **Add a target**: add a `[targets.<name>]` block in `foundry.toml` (see the
  [configuration reference](../reference/config.md)) and add the matching
  `DATABRICKS_*_<NAME>` secrets, then reference the target in a pipeline job.
- **Change what deploys**: the deploy verb takes `--only` / `--skip` to scope a
  deploy to specific bricks — see the [CLI reference](../reference/cli.md#deployment).
- **Point acceptance at another workspace**: set `DATABRICKS_HOST_*` secrets and
  `AGENT_FOUNDRY_TEST_CATALOG` on the GitHub/GitLab environment. Do not put a
  host URL in example manifests or `examples_registry.py`.
- **Add a custom scenario** (fork / extra instance): add instance files +
  `tests/live/` checks, then either rely on `testpaths` or overlay
  `agent_foundry/testing/scenarios.toml` via `AGENT_FOUNDRY_ACCEPTANCE_SCENARIOS`
  / a local catalog. The init registry stays display-only.

The **framework** repository uses a different, ephemeral pipeline
(`.github/workflows/acceptance.yml`, "Framework acceptance"): PRs into `release`
plus `workflow_dispatch`, protected `framework-acceptance` environment with
client-secret or OIDC auth, brick / payload / example canaries in parallel after the matrix, each exact-wheel `init` →
deploy → live checks, then leave the canary deployed. The next trigger destroys those stable names and redeploys. That path is not run on ordinary PRs to `main`.
Platform teams forking this repo: [Fork for platform teams](fork-for-platform-teams.md).

For the end-to-end first run (init → configure → deploy), see the
[getting-started tutorial](../tutorials/getting-started.md). The root
[README CI section](../../README.md#4-ci-deploys) has the same flow at a glance.
