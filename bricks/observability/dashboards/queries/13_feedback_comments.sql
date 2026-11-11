-- Recent free-text feedback comments from extended feedback.
-- Uses :time_range.min / :time_range.max DATE RANGE parameter.
SELECT
    a.last_update_time AS submitted_at,
    SHA2(a.source.source_id, 256) AS user_hash,
    a.feedback.value AS comment
FROM `${var.catalog}`.`${var.schema}`.mlflow_experiment_trace_metadata
LATERAL VIEW EXPLODE(assessments) AS a
WHERE a.source.source_type = 'HUMAN'
  AND a.assessment_name = 'user_feedback_comment'
  AND a.feedback.value IS NOT NULL AND TRIM(a.feedback.value) != ''
  AND (DATE(a.last_update_time) BETWEEN :time_range.min AND :time_range.max OR :time_range IS NULL)
ORDER BY a.last_update_time DESC
LIMIT 100
