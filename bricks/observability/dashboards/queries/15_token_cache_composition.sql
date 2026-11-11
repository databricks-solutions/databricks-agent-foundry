-- Daily input-token composition, in long form so the bar chart can stack it.
-- Four components, each priced differently, which is the whole point of the tab:
-- a request can grow enormous in tokens while barely growing in cost, provided
-- the growth lands in the cache-read bucket.
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
daily AS (
    SELECT DATE_TRUNC('DAY', request_time) AS request_date,
        SUM(cache_read) AS cache_read,
        SUM(write_5m) AS write_5m,
        SUM(write_1h) AS write_1h,
        SUM(uncached) AS uncached
    FROM priced
    GROUP BY 1
)
SELECT request_date, component, tokens
FROM daily
LATERAL VIEW STACK(
    4,
    'Cache read (0.1x)', cache_read,
    'Rolling write (1.25x)', write_5m,
    'Static write (2x)', write_1h,
    'Uncached (1.0x)', uncached
) AS component, tokens
ORDER BY request_date, component
