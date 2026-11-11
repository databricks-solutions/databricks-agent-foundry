-- Modelled billed units against the uncached-equivalent input, per day.
-- The gap between the two lines is what prompt caching is buying.  When caching
-- is off, or when the static prefix stops being byte-stable, the lines converge.
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
),
date_spine AS (
    SELECT EXPLODE(SEQUENCE(CAST(:time_range.min AS DATE), CAST(:time_range.max AS DATE), INTERVAL 1 DAY)) AS request_date
),
daily AS (
    SELECT DATE_TRUNC('DAY', request_time) AS request_date,
        ROUND(SUM(billed_units)) AS billed_units,
        SUM(prompt_tokens) AS uncached_equivalent
    FROM billed
    GROUP BY 1
)
SELECT d.request_date,
    COALESCE(t.billed_units, 0) AS billed_units,
    COALESCE(t.uncached_equivalent, 0) AS uncached_equivalent
FROM date_spine d LEFT JOIN daily t ON d.request_date = t.request_date
ORDER BY d.request_date
