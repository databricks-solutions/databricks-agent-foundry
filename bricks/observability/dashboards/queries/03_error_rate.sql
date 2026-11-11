-- Daily error rate: ratio of non-OK traces to total traces.
-- Uses :time_range.min / :time_range.max DATE RANGE parameter (default: last 30 days).
SELECT
    DATE_TRUNC('DAY', request_time) AS request_date,
    COUNT(*) AS total_requests,
    SUM(CASE WHEN state != 'OK' THEN 1 ELSE 0 END) AS error_count,
    ROUND(
        SUM(CASE WHEN state != 'OK' THEN 1 ELSE 0 END) * 100.0 / COUNT(*),
        2
    ) AS error_rate_pct
FROM `${var.catalog}`.`${var.schema}`.mlflow_experiment_trace_unified
WHERE (DATE(request_time) BETWEEN :time_range.min AND :time_range.max OR :time_range IS NULL)
GROUP BY 1
ORDER BY request_date
