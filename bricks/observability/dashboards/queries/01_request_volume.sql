-- Daily request volume with zero-fill for days without activity.
-- Uses :time_range.min / :time_range.max DATE RANGE parameter (default: last 30 days).
WITH date_spine AS (
    SELECT EXPLODE(SEQUENCE(CAST(:time_range.min AS DATE), CAST(:time_range.max AS DATE), INTERVAL 1 DAY)) AS request_date
),
daily AS (
    SELECT DATE_TRUNC('DAY', request_time) AS request_date, COUNT(*) AS request_count
    FROM `${var.catalog}`.`${var.schema}`.mlflow_experiment_trace_unified
    WHERE (DATE(request_time) BETWEEN :time_range.min AND :time_range.max OR :time_range IS NULL)
    GROUP BY 1
)
SELECT d.request_date, COALESCE(t.request_count, 0) AS request_count
FROM date_spine d LEFT JOIN daily t ON d.request_date = t.request_date
ORDER BY d.request_date
