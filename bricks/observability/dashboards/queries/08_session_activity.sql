-- ds_sessions: Daily distinct sessions and average traces per session
WITH date_spine AS (
  SELECT EXPLODE(SEQUENCE(CAST(:time_range.min AS DATE), CAST(:time_range.max AS DATE), INTERVAL 1 DAY)) AS request_date
),
daily AS (
  SELECT DATE_TRUNC('DAY', request_time) AS request_date,
    COUNT(DISTINCT trace_metadata:['mlflow.trace.session']::string) AS unique_sessions,
    ROUND(COUNT(*) * 1.0 / NULLIF(COUNT(DISTINCT trace_metadata:['mlflow.trace.session']::string), 0), 1) AS avg_traces_per_session
  FROM `${var.catalog}`.`${var.schema}`.mlflow_experiment_trace_unified
  WHERE (DATE(request_time) BETWEEN :time_range.min AND :time_range.max OR :time_range IS NULL)
  GROUP BY 1
)
SELECT d.request_date, COALESCE(t.unique_sessions, 0) AS unique_sessions,
  COALESCE(t.avg_traces_per_session, 0) AS avg_traces_per_session
FROM date_spine d LEFT JOIN daily t ON d.request_date = t.request_date
ORDER BY d.request_date
