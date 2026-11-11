-- Daily tool call counts with one series per tool (zero-filled).
-- TOOL spans carry the real tool name in spanInputs JSON; RETRIEVER spans use span.name.
-- Genie tool names are normalised (query_space_*, poll_response_* -> genie_query, genie_poll).
-- Uses :time_range.min / :time_range.max DATE RANGE parameter (default: last 30 days).
WITH date_spine AS (
    SELECT EXPLODE(SEQUENCE(CAST(:time_range.min AS DATE), CAST(:time_range.max AS DATE), INTERVAL 1 DAY)) AS request_date
),
tool_spans AS (
    SELECT
        DATE_TRUNC('DAY', request_time) AS request_date,
        CASE
            WHEN CAST(span.attributes:['mlflow.spanType'] AS STRING) = 'TOOL' THEN
                COALESCE(
                    GET_JSON_OBJECT(CAST(span.attributes:['mlflow.spanInputs'] AS STRING), '$.tool_name'),
                    span.name
                )
            ELSE span.name
        END AS raw_tool_name
    FROM `${var.catalog}`.`${var.schema}`.mlflow_experiment_trace_unified
    LATERAL VIEW EXPLODE(spans) AS span
    WHERE (DATE(request_time) BETWEEN :time_range.min AND :time_range.max OR :time_range IS NULL)
      AND CAST(span.attributes:['mlflow.spanType'] AS STRING) IN ('TOOL', 'RETRIEVER')
),
normalised AS (
    SELECT request_date,
        CASE
            WHEN raw_tool_name LIKE 'query_space_%' THEN 'genie_query'
            WHEN raw_tool_name LIKE 'poll_response_%' THEN 'genie_poll'
            ELSE REPLACE(raw_tool_name, '__', '.')
        END AS tool_name
    FROM tool_spans
    WHERE raw_tool_name IS NOT NULL
),
tools AS (SELECT DISTINCT tool_name FROM normalised),
grid AS (SELECT d.request_date, t.tool_name FROM date_spine d CROSS JOIN tools t),
daily AS (
    SELECT request_date, tool_name, COUNT(*) AS call_count
    FROM normalised GROUP BY 1, 2
)
SELECT g.request_date, g.tool_name, COALESCE(d.call_count, 0) AS call_count
FROM grid g LEFT JOIN daily d ON g.request_date = d.request_date AND g.tool_name = d.tool_name
ORDER BY g.request_date, g.tool_name
