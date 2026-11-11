-- ds_prompt_versions: Daily trace count by prompt version
SELECT
  DATE_TRUNC('DAY', request_time) AS request_date,
  CONCAT('v', COALESCE(GET_JSON_OBJECT(tags['mlflow.linkedPrompts'], '$[0].version'), '?')) AS prompt_version,
  COUNT(*) AS trace_count
FROM `${var.catalog}`.`${var.schema}`.mlflow_experiment_trace_unified
WHERE (DATE(request_time) BETWEEN :time_range.min AND :time_range.max OR :time_range IS NULL)
  AND tags['mlflow.linkedPrompts'] IS NOT NULL
GROUP BY 1, 2
ORDER BY request_date, prompt_version
