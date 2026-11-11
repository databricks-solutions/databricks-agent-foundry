# bricks/evaluation/

> **Technical reference** for deployers and developers.

Systematic evaluation and quality assurance for deployed agents. Provides a reusable scorer catalog, a development evaluation runner, and production monitoring registration -- all driven by a shared config.

```mermaid
flowchart TB
    subgraph devLoop [Dev Evaluation Loop]
        GoldenSet["Golden Dataset\n(YAML / UC)"] --> Runner["run_evaluation.py\n--mode direct"]
        Runner --> DevExp["MLflow Experiment:\nagent_evaluation"]
    end

    subgraph prodLoop [Production Loop]
        User --> ChatUI["Chat UI"]
        ChatUI --> AgentServer["MLflow AgentServer"]
        AgentServer --> ProdExp["MLflow Experiment:\nagent_traces_uc"]
        ChatUI -->|"thumbs up/down"| ProdExp
        ProdExp --> ProdMon["Production Monitoring\nScorers"]
    end

    subgraph dashboard [Observability Dashboard]
        ProdMon --> QualityChart["Quality Scores (CODE)"]
        ProdExp --> FeedbackChart["Human Feedback (HUMAN)"]
    end
```

## Two Experiments

The evaluation brick uses **two separate MLflow experiments**:

| Config key | Experiment | Purpose |
|------------|-----------|---------|
| `experiment_name` | `agent_evaluation` | Dev evaluation runs (`--mode direct`) |
| `production_experiment` | `agent_traces_uc` | Live traces, trace-based eval (`--mode traces`), and production monitoring |

The `production_experiment` must match the agent's `tracing_experiment` (computed by `InstanceConfig` from `foundry.toml` + the active target).

## Execution — Laptop-Triggered Against the Deployed App

`mlflow.genai.evaluate()` runs on the **developer's laptop** and calls the *deployed* agent over HTTP `/invocations`. Authentication is the developer's interactive user OAuth session (from `databricks auth login`), which carries the app's `.auth/callback` session cookie. That means `gap-auth` forwards the real user identity into the agent runtime, so OBO-dependent skills (e.g. a sample agent's `fn_my_epics`) resolve correctly.

### Trace-based flow (default)

For each golden-set question, `run_evaluation.py` does the following:

1. `POST /invocations` with header `x-mlflow-return-trace-id: true`. The MLflow AgentServer returns the deployed trace ID in `response.metadata.trace_id` ([mlflow/mlflow#20387](https://github.com/mlflow/mlflow/pull/20387), MLflow 3.11+).
2. Poll `mlflow.get_trace(trace_id)` — the deployed agent writes its trace to **UC OTel tables** (the destination is computed by `InstanceConfig` from `foundry.toml` and surfaced as `TRACING_UC_DESTINATION` in `app/app.yaml`), which are queried through `MLFLOW_TRACING_SQL_WAREHOUSE_ID`.
3. Pass records of shape `{inputs, expectations, trace}` to `mlflow.genai.evaluate(data=...)`. When `data` contains a populated `trace` column, the harness skips its predict loop and feeds the deep trace straight to the scorers — so `tool_routing_accuracy`, `genie_sql_correctness`, and `RetrievalGroundedness` see real TOOL / RETRIEVER spans instead of a shallow HTTP wrapper.

This replaces the earlier `predict_fn=deployed` flow, which produced a shallow span that caused `tool_routing_accuracy` and `genie_sql_correctness` to silently skip.

### Known risks

- **Cross-experiment trace copy** (`PERMISSION_DENIED` on `copy_trace_to_experiment`). The deployed trace's source experiment must match the eval run's experiment, otherwise the harness copy-to-experiment call fails. `run_evaluation.py` reuses the agent's `tracing_experiment` (computed by `InstanceConfig`) for the eval experiment name, which keeps source and target experiment IDs identical. Additionally, UC-backed traces short-circuit `_should_clone_trace` in the MLflow harness, so no copy is attempted at all.
- **Terminated tracing warehouse** (pre MLflow 3.12 `mlflow.get_trace` silently returns `None` when the tracing warehouse is terminated). `run_evaluation.py` calls `start_and_wait` on `MLFLOW_TRACING_SQL_WAREHOUSE_ID` before evaluating.
- No CI-style serverless-job path: the Apps OAuth proxy rejects the bearer-only tokens a Databricks Job carries. Laptop OBO remains the supported path.

### HTTP timeout and Genie QPM

Genie Spaces are throttled at **20 QPM per workspace**. During an eval batch (50+ questions in a minute, often 2 Genie calls each), the per-call latency inflates from ~20s steady-state to ~150s under queueing — pushing total trace time past a tight HTTP timeout. Observed distribution on a live workload: p50 3s, p95 191s, p99 305s.

HTTP read timeout defaults to **360s** (covers p99 with headroom). Override with `EVAL_HTTP_TIMEOUT_SECONDS` for agents with different latency profiles:

```bash
EVAL_HTTP_TIMEOUT_SECONDS=600 uv run agent-foundry eval --instance <name>
```

For agents that make many Genie calls, reduce per-turn pressure by instructing the agent to cap Genie calls (e.g. 2 per turn) in its system prompt.

## Scorer Catalog

| Scorer | Type | Dev | Prod | What It Checks |
|--------|------|-----|------|---------------|
| `Safety` | Built-in LLM | Yes | Yes | Harmful/offensive content |
| `RelevanceToQuery` | Built-in LLM | Yes | Yes | Response relevance |
| `RetrievalGroundedness` | Built-in LLM | Opt-in | No | Grounded in retrieved context |
| `Correctness` | Built-in LLM | Yes | No | Matches expected answer |
| `ToolCallCorrectness` | Built-in LLM | Opt-in | No | Correct tool calls (see note below) |
| `tool_routing_accuracy` | Custom code | Yes | **No** | Agent routed to expected tools (needs `expectations`) |
| `genie_sql_correctness` | Custom code | Yes | **No** | Genie produced expected SQL (needs `expectations`) |
| `response_quality` | Custom code | Yes | Yes | Length, sources, latency |

Scorers marked **No** for prod require `expectations` (ground truth), which is unavailable for live traces.

> **Note on `ToolCallCorrectness`:** Degraded on MLflow 3.11.x -- the built-in LLM tool-extractor fails pydantic validation on every trace. It is intentionally excluded from the default `with_ground_truth` preset; prefer `tool_routing_accuracy` for deterministic tool-routing signal. Instances that want the built-in judge can opt in by adding `- ToolCallCorrectness` to their own `instances/<name>/evaluation/eval_config.yml`.
>
> **Note on `RetrievalGroundedness`:** The built-in judge errors on every row when the trace has no `RETRIEVER` span. It is excluded from the default `traces_only` preset. Instances with a vector-search / RAG skill that emit retriever spans can opt in by adding `- RetrievalGroundedness` under `scorers.traces_only`.

## Human Feedback

The observability dashboard includes a **Human Feedback** chart that shows thumbs-up/down votes from the chat UI. These are stored as `HUMAN`-sourced assessments on MLflow traces. The quality scores chart shows `CODE`-sourced assessments from automated scorers.

## Dataset format

Golden sets are YAML files under `instances/<name>/evaluation/datasets/` (see `datasets/_template.yml`) or a UC `EvaluationDataset` passed with `--dataset`. Each row has `inputs` with a `question`, and optional `expectations` for ground-truth scorers.

A row can also carry request-level inputs. `inputs.custom_inputs` and `inputs.context` are optional mappings that are sent as `ResponsesAgentRequest.custom_inputs` / `.context` in both `--predict-via deployed` and `local`. Rows without them send exactly `{"input": [...], "stream": false}`, as before. They are not `${var.*}`-templated.

```yaml
dataset:
  - inputs:
      question: Remove the oil temperature histogram from the report.
      custom_inputs:            # seeds request-level state the agent reads
        report_spec: {...}
      context:
        conversation_id: eval-123
    expectations:
      expected_tools: []
```

Built-in judges that read `inputs` (`RelevanceToQuery`, `Correctness`) get the whole `inputs` dict as their request text, so a large seed shows up in the judge prompt next to the question.

## Quick Start

```bash
# Laptop eval (default: hits the deployed app with real user OBO)
uv run agent-foundry eval --instance <your-app> --target dev

# In-process deep-trace eval (no OBO) — opt in explicitly
uv run agent-foundry eval --instance <your-app> --target dev --predict-via local

# Score already-logged production traces
uv run agent-foundry eval --instance <your-app> --target dev --mode traces --max-traces 50

# Production monitoring
uv run agent-foundry monitors start  --instance <your-app> --target dev
uv run agent-foundry monitors status --instance <your-app> --target dev
```

## Contents

| Path | Purpose |
|------|---------|
| `conf/eval_config.yml` | Scorer selection, experiment split, monitoring settings |
| `scorers/` | Reusable scorer functions (built-in + custom) |
| `scripts/run_evaluation.py` | Laptop eval CLI (user OBO via `/invocations`) |
| `scripts/register_monitors.py` | Production monitoring CLI |
| `datasets/_template.yml` | Example evaluation dataset format |
| `_scaffold/datasets/smoke.yml` | One greeting record copied with the eval config |
| `tests/` | Offline unit tests |
