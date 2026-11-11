-- The 100 most token-hungry requests, split by how each token was priced.
-- One row per trace, so a tool-calling loop that made ten LLM calls appears once
-- with llm_calls = 10.  Copy the trace_id into the MLflow trace UI to drill in.
--
-- Uses :time_range.min / :time_range.max DATE RANGE parameter (default: last 30 days).
WITH llm AS (
    SELECT
        trace_id,
        request_time,
        execution_duration_ms,
        COALESCE(trace_metadata:['mlflow.trace.user']::string, trace_metadata:['mlflow.user']::string) AS raw_user,
        CAST(span.attributes:['mlflow.spanOutputs']:model AS STRING) AS model,
        COALESCE(CAST(span.attributes:['mlflow.spanOutputs']:usage:prompt_tokens AS BIGINT), 0) AS prompt_tokens,
        COALESCE(CAST(span.attributes:['mlflow.spanOutputs']:usage:completion_tokens AS BIGINT), 0) AS output_tokens,
        COALESCE(CAST(span.attributes:['mlflow.spanOutputs']:usage:cache_read_input_tokens AS BIGINT), CAST(span.attributes:['mlflow.chat.tokenUsage']:cache_read_input_tokens AS BIGINT), 0) AS cache_read,
        COALESCE(CAST(span.attributes:['mlflow.spanOutputs']:usage:cache_creation_input_tokens AS BIGINT), CAST(span.attributes:['mlflow.chat.tokenUsage']:cache_creation_input_tokens AS BIGINT), 0) AS cache_write,
        COALESCE(CAST(span.attributes:['mlflow.spanOutputs']:usage:cache_creation:ephemeral_1h_input_tokens AS BIGINT), 0) AS write_1h
    FROM `${var.catalog}`.`${var.schema}`.mlflow_experiment_trace_unified
    LATERAL VIEW EXPLODE(spans) AS span
    WHERE (DATE(request_time) BETWEEN :time_range.min AND :time_range.max OR :time_range IS NULL)
      AND span.attributes:['mlflow.chat.tokenUsage'] IS NOT NULL
),
priced AS (
    SELECT *,
        GREATEST(cache_write - write_1h, 0) AS write_5m,
        GREATEST(prompt_tokens - cache_read - cache_write, 0) AS uncached
    FROM llm
),
billed AS (
    SELECT *,
        0.1 * cache_read + 1.25 * write_5m + 2.0 * write_1h + uncached AS billed_units
    FROM priced
)
SELECT
    MIN(request_time) AS request_time,
    CONCAT('user-', LEFT(SHA2(COALESCE(MAX(raw_user), 'unknown'), 256), 8)) AS user_id,
    COUNT(*) AS llm_calls,
    SUM(prompt_tokens) AS input_tokens,
    SUM(cache_read) AS cache_read,
    SUM(write_5m) AS write_5m,
    SUM(write_1h) AS write_1h,
    SUM(uncached) AS uncached,
    ROUND(SUM(billed_units)) AS billed_units,
    ROUND(100.0 * (1 - SUM(billed_units) / NULLIF(SUM(prompt_tokens), 0)), 1) AS savings_pct,
    SUM(output_tokens) AS output_tokens,
    trace_id
FROM billed
GROUP BY trace_id
ORDER BY input_tokens DESC
LIMIT 100
