-- Feedback category distribution from extended feedback.
-- Uses :time_range.min / :time_range.max DATE RANGE parameter.
SELECT TRIM(cat) AS category, COUNT(*) AS count
FROM `${var.catalog}`.`${var.schema}`.mlflow_experiment_trace_metadata
LATERAL VIEW EXPLODE(assessments) AS a
LATERAL VIEW EXPLODE(SPLIT(a.feedback.value, ',')) AS cat
WHERE a.source.source_type = 'HUMAN'
  AND a.assessment_name = 'user_feedback_categories'
  AND (DATE(a.last_update_time) BETWEEN :time_range.min AND :time_range.max OR :time_range IS NULL)
GROUP BY 1
ORDER BY count DESC
