-- Which tools put the most text into the context, ranked.
--
-- Caching makes a token cheap to re-send but never free to introduce: every new
-- tool result is written at 1.25x and then re-read on each subsequent LLM call in
-- the loop.  Once the static prefix is cached, this is usually where the cost is,
-- so this panel is the feedback loop for row limits and result shaping.
--
-- Token counts are estimated at four characters per token.
-- Uses :time_range.min / :time_range.max DATE RANGE parameter (default: last 30 days).
WITH tool_spans AS (
    SELECT
        COALESCE(
            GET_JSON_OBJECT(CAST(span.attributes:['mlflow.spanInputs'] AS STRING), '$.tool_name'),
            span.name
        ) AS raw_tool_name,
        LENGTH(CAST(span.attributes:['mlflow.spanOutputs'] AS STRING)) AS result_chars
    FROM `${var.catalog}`.`${var.schema}`.mlflow_experiment_trace_unified
    LATERAL VIEW EXPLODE(spans) AS span
    WHERE (DATE(request_time) BETWEEN :time_range.min AND :time_range.max OR :time_range IS NULL)
      AND CAST(span.attributes:['mlflow.spanType'] AS STRING) = 'TOOL'
)
SELECT
    REPLACE(raw_tool_name, '__', '.') AS tool_name,
    COUNT(*) AS call_count,
    ROUND(AVG(result_chars)) AS avg_chars,
    MAX(result_chars) AS max_chars,
    ROUND(SUM(result_chars) / 4.0) AS est_total_tokens
FROM tool_spans
WHERE raw_tool_name IS NOT NULL
GROUP BY 1
ORDER BY est_total_tokens DESC
