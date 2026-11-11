# bricks/tracing/

> **Technical reference** for deployers and developers.

Deploy-time brick that provisions the Unity Catalog-backed **MLflow experiment**
used for agent tracing. The request-path runtime lives in the agent payload
([`payloads/agent/runtime/`](../../payloads/agent/runtime/README.md)). The split
keeps runtime code from importing deploy-time code, so the runtime
can ship as a separate wheel.

> Registered as the `tracing` deploy brick (`MlflowExperimentBrick`,
> `deploy_order = 40`). Renamed from the misnamed `agent` brick; the
> deprecated `agent` CLI alias was removed in agent-foundry 2.0. Its CLI
> verb is `tracing` (see [Deploy & destroy](#deploy--destroy)).

## Contents

| File | Purpose |
|------|---------|
| `brick.py` | `MlflowExperimentBrick` — deploy/destroy adapter and the `MlflowExperimentState` contract |
| `scripts/ensure_experiment.py` | Idempotent upsert of the UC-backed MLflow experiment (and `destroy_experiment`) |
| `tests/unit/test_destroy_experiment.py` | Offline unit tests for the destroy path |

## What it does

`ensure_experiment` creates (or reuses) an MLflow experiment bound to a Unity
Catalog trace location. It is an **upsert** — safe to run repeatedly — and also
records the SQL warehouse ID that production monitoring scorers use to query the
UC trace tables. It runs early in the deploy pipeline (before the Agent App) so
the experiment exists when the app and dashboard try to resolve it.

## Configuration

Tracing configuration is **computed, not authored**: `tracing_experiment`,
`tracing_destination`, `tracing_table_prefix`, and `tracing_warehouse_id` are
derived by `agent_foundry.config.InstanceConfig` from `foundry.toml` and the
active target. The brick `applies_to` an instance when that instance's `payload.yml`
lists `tracing` under `payload.requires`. REST-to-MCP proxies omit it.
If the payload requires tracing and the instance cannot provide
it (no `mlflow`, no `tracing_experiment`, or `--skip tracing` on a full
deploy), deploy errors instead of silently continuing.

## State

The experiment ID is persisted to the **workspace-backed** state store (the
single source of truth shared across users and CI — see
[`agent_foundry/state.py`](../../agent_foundry/state.py)) under
`brick_type="tracing"`, validated against the `MlflowExperimentState` contract:

```json
{ "experiment_id": "1234567890" }
```

If state is missing, destroy recovers the experiment by name + `managed_by` tag,
so no local cache is required.

## Deploy & destroy

```bash
# As part of a full deploy (recommended)
uv run agent-foundry deploy --target dev --instance <your-app>

# Standalone
uv run agent-foundry brick tracing ensure-experiment --instance <your-app> --target dev
```

`deploy` creates the trace schema (`tracing_destination`) when it is missing;
MLflow then creates the trace tables in it.

`destroy` is staged: it removes the MLflow experiment (non-destructive) and, with
`--force`, drops the UC trace tables and then the trace schema (destructive; the
schema drop uses `RESTRICT` so user tables are preserved).
