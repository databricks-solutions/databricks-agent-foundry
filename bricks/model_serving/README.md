# Model Serving brick

The Model Serving brick deploys serverless Model Serving endpoints that serve a
Unity Catalog model. It covers both ends of the range: a classic ML model
(scikit-learn, XGBoost, a custom pyfunc) on CPU, and a large model on a GPU that
the brick builds itself with a serverless job (for example the Whisper speech
model behind the agent's mic button).

## Instance layout

```text
model_serving/
  churn/
    endpoint_config.yml
  speech/
    endpoint_config.yml
    build_whisper.py
    whisper_model.py
```

Each direct child of `model_serving/` is an independent sub-instance; the
presence of `endpoint_config.yml` opts it into `agent-foundry deploy`. The
brick deploys after Vector Search and before Jobs and the App (`deploy_order`
65), so the App can reference the endpoint.

## Configuration surface

`agent-foundry scaffold model_serving` writes `model_serving/example_model/endpoint_config.yml`,
which documents every `ServingEndpointConfig` field. The JSON Schema is
`model_serving_endpoint_config.schema.json`.

| Field | Default | Meaning |
|-------|---------|---------|
| `endpoint_name` | — | Workspace-unique endpoint name |
| `model.name` | — | Three-level UC model name |
| `model.version` / `model.alias` | latest version | Which version to serve (at most one) |
| `model.workload_type` | `CPU` | `CPU*`, `GPU_SMALL` (1× T4), `GPU_MEDIUM`, `GPU_LARGE`, `MULTIGPU_MEDIUM` |
| `model.workload_size` | `Small` | `Small` (4), `Medium` (8–16), `Large` (16–64) concurrent requests |
| `model.scale_to_zero` | `true` | No idle cost, but a cold start on the first request |
| `model.environment_vars` | — | Container environment (secret references allowed) |
| `build` | — | Optional serverless build job (below) |
| `tags` | — | Extra endpoint tags; managed-by tags are always added |

### Serving an existing model (CPU)

```yaml
endpoint_name: ${var.app_name}-churn
model:
  name: ${var.catalog}.${var.schema}.churn_classifier
  alias: champion
```

### Building the model (GPU)

With `build`, the brick uploads the script and its `files` to
`/Shared/agent-foundry/<target>/<app>/model_serving/<name>/` and runs the script
as a one-off serverless job with the listed `dependencies`. The script gets
`--model-name <model.name>` and `--experiment <workspace path>` and must
register exactly one new version. Serverless runs the script through `exec()`,
so `__file__` is not set; find the uploaded `files` next to it through a
function's `__code__.co_filename`. Signal failure by raising, never with
`sys.exit` or `SystemExit`: the job fails on any `SystemExit`, even 0. The brick serves that version and records a
hash of the build inputs; the next deploy rebuilds only when the script, a file
or a dependency changes, or the recorded version is gone.

```yaml
endpoint_name: ${var.app_name}-speech
model:
  name: ${var.catalog}.${var.schema}.whisper_large_v3_turbo
  workload_type: GPU_SMALL
  scale_to_zero: false
build:
  script: ./build_whisper.py
  files: [./whisper_model.py]
  dependencies: [faster-whisper==1.2.1, mlflow==3.17.1]
  timeout_minutes: 60
```

`build` cannot be combined with `model.version` or `model.alias`: the build
decides the version.

## Outputs

| Variable | Value |
|----------|-------|
| `serving_endpoint_name__<name>` | The endpoint name, for example for `payload.yml` |

## Deploy and destroy

```bash
agent-foundry deploy --instance <app> --target dev
agent-foundry brick model_serving deploy --instance <app>/speech --target dev
agent-foundry destroy --instance <app> --target dev --force
```

Creating an endpoint waits until it is ready (up to 60 minutes; GPU endpoints
typically take 20–40). When it fails, the brick prints the tail of the
container build log. Updating an endpoint that already serves the desired
version and compute is a no-op.

Destroy deletes the endpoint. For a built model it also deletes the
registered model with all its versions and the build experiment: the brick
owns what it builds. A model you register yourself is never deleted.

## Out of scope

Provisioned-throughput foundation models, external models, traffic splits
across several served entities, AI Gateway settings and inference tables.
