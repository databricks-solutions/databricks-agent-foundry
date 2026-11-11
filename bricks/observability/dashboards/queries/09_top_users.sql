-- ds_top_users: Per-user breakdown with request count, latency, tokens, last active.
-- User names are hashed (SHA-256, first 8 chars) for GDPR compliance.
SELECT
  CONCAT('user-', LEFT(SHA2(raw_user, 256), 8)) AS user_id,
  COUNT(*) AS request_count,
  ROUND(AVG(CAST(execution_duration_ms AS DOUBLE)), 0) AS avg_latency_ms,
  COALESCE(SUM(TRY_CAST(GET_JSON_OBJECT(trace_metadata:['mlflow.trace.tokenUsage']::string, '$.total_tokens') AS BIGINT)), 0) AS total_tokens,
  MAX(request_time) AS last_active
FROM (
  SELECT *, COALESCE(trace_metadata:['mlflow.trace.user']::string, trace_metadata:['mlflow.user']::string) AS raw_user
  FROM `${var.catalog}`.`${var.schema}`.mlflow_experiment_trace_unified
  WHERE (DATE(request_time) BETWEEN :time_range.min AND :time_range.max OR :time_range IS NULL)
)
WHERE raw_user IS NOT NULL
GROUP BY 1
ORDER BY request_count DESC
