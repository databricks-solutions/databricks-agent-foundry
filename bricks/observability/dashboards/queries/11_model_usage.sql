-- ds_model_usage: Daily trace count by model version
SELECT
  DATE_TRUNC('DAY', request_time) AS request_date,
  COALESCE(trace_metadata:['mlflow.modelId']::string, 'unlinked') AS model_id,
  COUNT(*) AS trace_count
FROM `${var.catalog}`.`${var.schema}`.mlflow_experiment_trace_unified
WHERE (DATE(request_time) BETWEEN :time_range.min AND :time_range.max OR :time_range IS NULL)
GROUP BY 1, 2
ORDER BY request_date, model_id
