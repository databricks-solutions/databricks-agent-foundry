-- Headline token and prompt-cache economics for the whole time range.
-- Returns exactly one row; six counters read it.
--
-- MLflow's own token counters report the uncached-equivalent input, so they look
-- identical whether or not caching worked.  Cache read/write totals are on the
-- LLM span: prefer spanOutputs.usage extras, fall back to mlflow.chat.tokenUsage
-- (MLflow 3.16.1+).  The 1h vs 5m write split exists only on nested
-- spanOutputs.usage.cache_creation and is not in tokenUsage.
--
-- Billed units are MODELLED from the published cache multipliers, not billed:
--   cache read 0.1x, rolling (5m) write 1.25x, static (1h) write 2x, uncached 1.0x
-- Output tokens are never discountable and stay a separate figure.
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
    COALESCE(SUM(prompt_tokens), 0) AS input_tokens,
    COALESCE(SUM(output_tokens), 0) AS output_tokens,
    ROUND(100.0 * SUM(cache_read) / NULLIF(SUM(prompt_tokens), 0), 1) AS cache_read_pct,
    ROUND(100.0 * (1 - SUM(billed_units) / NULLIF(SUM(prompt_tokens), 0)), 1) AS savings_pct,
    ROUND(SUM(billed_units) / NULLIF(SUM(prompt_tokens), 0), 3) AS effective_multiplier,
    ROUND(100.0 * SUM(CASE WHEN write_1h > 0 THEN 1 ELSE 0 END) / NULLIF(COUNT(DISTINCT trace_id), 0), 1) AS prefix_rebuilds_per_100
FROM billed
