-- Token economics per serving model.  Only Claude endpoints honour cache_control,
-- so a model with a 0% cache read rate next to one with 85% is usually a routing
-- finding rather than a caching bug.
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
    COALESCE(model, 'unknown') AS model,
    COUNT(*) AS llm_calls,
    SUM(prompt_tokens) AS input_tokens,
    ROUND(100.0 * SUM(cache_read) / NULLIF(SUM(prompt_tokens), 0), 1) AS cache_read_pct,
    ROUND(SUM(billed_units)) AS billed_units
FROM billed
GROUP BY 1
ORDER BY input_tokens DESC
