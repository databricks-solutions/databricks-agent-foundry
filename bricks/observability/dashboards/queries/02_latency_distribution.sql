-- Daily latency percentiles (p50, p95, p99) in seconds with zero-fill.
-- Uses :time_range.min / :time_range.max DATE RANGE parameter (default: last 30 days).
WITH date_spine AS (
    SELECT EXPLODE(SEQUENCE(CAST(:time_range.min AS DATE), CAST(:time_range.max AS DATE), INTERVAL 1 DAY)) AS request_date
),
daily AS (
    SELECT DATE_TRUNC('DAY', request_time) AS request_date,
        ROUND(PERCENTILE_APPROX(CAST(execution_duration_ms AS DOUBLE), 0.5) / 1000, 1) AS latency_p50_s,
        ROUND(PERCENTILE_APPROX(CAST(execution_duration_ms AS DOUBLE), 0.95) / 1000, 1) AS latency_p95_s,
        ROUND(PERCENTILE_APPROX(CAST(execution_duration_ms AS DOUBLE), 0.99) / 1000, 1) AS latency_p99_s
    FROM `${var.catalog}`.`${var.schema}`.mlflow_experiment_trace_unified
    WHERE (DATE(request_time) BETWEEN :time_range.min AND :time_range.max OR :time_range IS NULL)
    GROUP BY 1
)
SELECT d.request_date,
    COALESCE(t.latency_p50_s, 0) AS latency_p50_s,
    COALESCE(t.latency_p95_s, 0) AS latency_p95_s,
    COALESCE(t.latency_p99_s, 0) AS latency_p99_s
FROM date_spine d LEFT JOIN daily t ON d.request_date = t.request_date
ORDER BY d.request_date
