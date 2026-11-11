# Test Architecture

## Overview

One rule at every level: `tests/unit/` is offline, and `tests/live/` checks
a CLI deployment in a real workspace. Each owner keeps both folders next to
its code; only the repo-wide `tests/contracts/` and `tests/component/` sit
at the top level.

```
  Layer                     What runs                              Needs
  ───────────────────────── ────────────────────────────────────── ──────────────────
  Offline                   config, logic, dry-runs (tests/unit)   nothing
  Packaged example smoke    wheel + init + example tests/unit      uv, package index
  Live levels (canaries)    tests/live after a CLI deployment      workspace + approval
```

## Where tests live

| Owner | Offline | Live |
|---|---|---|
| Framework | `agent_foundry/tests/unit/` | -- |
| Brick | `bricks/<brick>/tests/unit/` | `bricks/<brick>/tests/live/` (brick canary) |
| Payload | `payloads/<payload>/tests/unit/<area>/` | `payloads/<payload>/tests/live/` (payload canary) |
| Example / generated instance | `instances/<name>/tests/unit/` | `instances/<name>/tests/live/` (example canary) |
| Repo-wide | `tests/contracts/`, `tests/component/` | -- |

Every test under a `tests/live/` folder is marked `live` automatically, so
test files carry no `pytestmark`. Live tests get the deployment through the
`live_target`, `live_ws` and `live_variables` fixtures; `gone` checks (after
`destroy`) read `gone_variables`. Markers are documented in
[docs/reference/cli.md](../docs/reference/cli.md#pytest-markers).

**Run**:
```bash
python -m pytest                           # all offline testpaths from pyproject.toml
python -m pytest agent_foundry/tests/unit  # framework (CLI, config, deploy, init)
python -m pytest tests/contracts           # repo-wide rules (imports, docs, registry, packaging)
python -m pytest tests/component --run-component
python -m pytest bricks/data/tests/unit    # one brick's unit tests
```

### What runs when

| Layer | What it proves | Trigger | CI job | Local command |
|---|---|---|---|---|
| Static checks | Format, lint, types | Every PR | `Static checks (ruff)`, `Static checks (mypy)` | `uv run ruff check . && uv run mypy` |
| Offline tests | Framework, brick, and payload logic; no workspace client. Agent UI TypeScript helpers (`payloads/agent/ui/tests/ts`) | Every PR | `Offline tests` | `uv run pytest -m "not live and not component"`; `npm test` in `payloads/agent/ui/tests/ts` |
| Packaged example smoke | Wheel installs; `init` of every shipped example; each generated instance's `tests/unit` passes | Every PR | `Packaged example smoke` | `uv run pytest -m component --run-component` |
| Framework acceptance | Consumer journey at the candidate SHA: `uvx … init` → `scaffold` → `deploy` → `wait` → `smoke`. Brick, payload, example, and **Agent serving live** start together after the matrix. Brick canary instead deploys each brick `deploy --only` then its `tests/live` checks, then `destroy` and `gone` checks. Report only (`continue-on-error`, never fails the run): payload tracing/observability and `agent-foundry eval` after smoke, and Serving live, which hits Model Serving from the runner (`payloads/agent/tests/live/runtime`), no App | Every PR into `release`, dispatch | `Brick canary`, `Payload canary`, `Example canary`, `Agent serving live` | See [Reproduce a canary locally](#reproduce-a-canary-locally) |

Every canary job waits on the protected `framework-acceptance` environment
(required reviewers; the environment's service-principal client secret, or
GitHub OIDC federation once the secret is removed), so each job needs its
own approval. Canary names are
stable (`af-<code>-canary`). The brick canary destroys the previous run
first; payload and example canaries stay deployed and the next run updates
them in place (dispatch with `reset` to destroy them first). Production promotion is not a
framework concern: the framework repo has no deployable instance, and
downstream repos promote through their generated `release-deploy-prod`
workflow.

## Reproduce a canary locally

The same steps the `canary-journey` action runs, with your own profile
instead of OIDC. Use your own run id: the default `canary` names belong to
CI.

```bash
databricks auth login --host https://<workspace> --profile <profile>
export DATABRICKS_HOST=https://<workspace> DATABRICKS_CONFIG_PROFILE=<profile>
export AGENT_FOUNDRY_TEST_CATALOG=<catalog>
export AGENT_FOUNDRY_TEST_VS_ENDPOINT=<existing vector search endpoint>  # brick canary only

# Run names, scaffolds, checks and the init answers for one scenario
python -m agent_foundry.testing.harness prepare --scenario bricks --run-id "$USER" > /tmp/prep.json
project=$(python -c 'import json; print(json.load(open("/tmp/prep.json"))["project_name"])')
python -c 'import json; print(json.load(open("/tmp/prep.json"))["stdin"], end="")' > /tmp/init.stdin

# Consumer path, from this checkout
uvx --from . agent-foundry init --outdir /tmp/generated --name "$project" --framework-ref "$(git rev-parse HEAD)" < /tmp/init.stdin
cd "/tmp/generated/$project"
uv run agent-foundry scaffold warehouse --instance "$project"   # repeat for each scaffold in prep.json
# Scenario files (fixture = true; agent_payload adds a SQL MCP, a skill and a smoke question)
uv run python -m agent_foundry.testing.harness apply-fixture --scenario bricks --instance-dir "instances/$project"
```

**Brick level**: deploy one brick, then run its checks from the framework
checkout against the generated project.

```bash
uv run agent-foundry deploy --instance "$project" --target dev --only warehouse
AGENT_FOUNDRY_PROJECT_ROOT="/tmp/generated/$project" \
  python -m pytest bricks/warehouse/tests/live --run-live      # from the framework checkout
uv run agent-foundry destroy --instance "$project" --target dev --force
AGENT_FOUNDRY_PROJECT_ROOT="/tmp/generated/$project" \
  python -m pytest bricks/warehouse/tests/live -m gone --run-live --run-gone
```

The vector index needs `--vars schema=<docs schema> --vars source_table=docs
--vars index_name=<name> --vars vs_endpoint_name=$AGENT_FOUNDRY_TEST_VS_ENDPOINT`
on its `deploy --only vector_indexes`.

**Payload and example levels**: `prepare --scenario agent_payload` (or an
example id), the same `init` and `scaffold` steps, then the three gate
commands inside the generated repo:

```bash
uv run agent-foundry deploy --instance "$project" --target dev
uv run agent-foundry wait --instance "$project" --target dev
uv run agent-foundry smoke --instance "$project" --target dev
```

Those three are the gate; reproduce a red canary with them alone. The
report-only tracing and observability checks run from the framework
checkout:

```bash
AGENT_FOUNDRY_PROJECT_ROOT="/tmp/generated/$project" \
  python -m pytest payloads/agent/tests/live/test_tracing.py \
    payloads/agent/tests/live/test_observability.py --run-live
```

**Serving live** (no App; CI job `Agent serving live`):

```bash
export FOUNDRY_REASONING_E2E_ENDPOINT=databricks-claude-sonnet-5-5
python -m pytest payloads/agent/tests/live/runtime --run-live
```

**Re-run only the checks** against a deployment that is still up (the
usual case after a red canary): the `smoke` or `pytest` line alone.

## CI pipeline mapping

```
                    ci.yml (every PR / push to main)
                  ┌──────────────────────────────────────────┐
                  │  Static checks (ruff, mypy)              │
push / PR ───────►│  Offline tests (+ offline validate)      │
                  │  Packaged example smoke  (-m component)  │
                  └──────────────────────────────────────────┘

                    acceptance.yml (Framework acceptance)
                  ┌──────────────────────────────────────────┐
PR → release ────►│  brick → payload → example canaries      │
manual ──────────►│  uvx init → deploy → wait → smoke (gate) │
                  │  tracing, eval, serving (report only)    │
                  └──────────────────────────────────────────┘
```

Forking this repo (runners, PyPI mirror, `framework-acceptance` reviewers):
[Fork for platform teams](../docs/how-to/fork-for-platform-teams.md).

## Environment variables

| Variable | Used by | Source |
|---|---|---|
| `DATABRICKS_HOST` | live checks | env (GitHub Environment secret) or your shell |
| `DATABRICKS_CLIENT_ID` + `DATABRICKS_CLIENT_SECRET` | canary jobs | `framework-acceptance` secrets; without the client secret, jobs set `DATABRICKS_AUTH_TYPE=github-oidc` (needs a federation policy, see [Deploy via CI/CD](../docs/how-to/deploy-cicd.md#workspace-auth-client-secret-or-oidc)) |
| `DATABRICKS_CONFIG_PROFILE` | local live checks | `databricks auth login` (local only; do not auto-select in CI) |
| `AGENT_FOUNDRY_TEST_CATALOG` | every canary (init catalog answer) | env / GitHub Environment variables |
| `AGENT_FOUNDRY_TEST_VS_ENDPOINT` | brick canary (vector index endpoint, kept between runs) | env / GitHub Environment variables |
| `AGENT_FOUNDRY_TEST_MODEL_SERVICE` | serving live: Unity Gateway model-service variants (unset skips them) | env / GitHub Environment variables |
| `AGENT_FOUNDRY_REQUIRE_LIVE` | canary jobs | `1` turns a missing binding / instance into a failure |
| `AGENT_FOUNDRY_PROJECT_ROOT` | live checks run from the framework checkout | path to the generated project |

## Shared test infrastructure

| Module | Purpose |
|---|---|
| `agent_foundry.pytest_plugin` | `--run-*` flags, markers, the `tests/live` marker, live fixtures (pytest11 entry point) |
| `agent_foundry.testing` | Scenario catalog, workspace binding, live helpers (`run_sql`, `published_outputs`), readiness, Responses API contracts (shipped in the wheel) |
| `agent_foundry.testing.fakes` | `FakeWorkspaceClient`, the in-memory `WorkspaceClient` for brick and payload deploy unit tests |
| `instances/*/tests/unit/conftest.py` | Instance-scoped offline fixtures: `instance_dir`, `instance_config` |

## Contract mixins

`BasicInvocationContract` and `ResponseFormatContract` in
`agent_foundry.testing.responses` define the shared Responses API contract.
Deployed Apps use `tests/live/` checks (MCP/A2A), not Model Serving.

To add a new transport check, inherit the mixins:

```python
class TestCanaryInvocation(BasicInvocationContract):
    @pytest.fixture(scope="class")
    def simple_response(self, canary_url):
        return requests.post(f"{canary_url}/invocations", json=payload)
```

## Directory layout

```
agent_foundry/tests/unit/                # framework: CLI, config, deploy, init, pytest plugin
bricks/<brick>/tests/unit/               # one home per brick
bricks/<brick>/tests/live/               #   brick canary checks (+ test_gone.py)
payloads/agent/tests/unit/               # agent payload: config, contracts
  runtime/                               #   runtime config, tools, MCP consumption
  deploy/                                #   lakebase, env overrides, MCP step
    mcp/                                 #   MCP server config + URL resolution
    skills/                              #   skill assembly + validation
payloads/agent/tests/live/               #   tracing, observability (the App itself: agent-foundry smoke)
  runtime/                               #   Agent serving live: reasoning + prompt-cache (not the App)
payloads/rest_mcp/tests/unit/            # rest_mcp payload: contracts, scaffold, schema
  runtime/ runtime/server/ deploy/
payloads/rest_mcp/tests/live/            #   UC gateway checks (opt-in)
examples/<example>/tests/unit/           # example config checks (packaged smoke)
examples/<example>/smoke.yml             #   extra questions for agent-foundry smoke
tests/
  contracts/                             # repo-wide rules: imports, docs, registry, packaging
  component/                             # packaged CLI / wheel smokes
  standalone_repo.py                     # helper shared by component + contracts
```
