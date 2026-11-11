-- Average quality scores over time from MLflow assessments.
-- Uses :time_range.min / :time_range.max DATE RANGE parameter (default: last 30 days).
-- Assessments are logged by MLflow scorers (e.g. RelevanceToQuery, Safety).
SELECT
    DATE_TRUNC('DAY', a.last_update_time) AS score_date,
    a.assessment_name AS scorer,
    AVG(
        CASE
            WHEN a.feedback.value IN ('true', 'yes') THEN 1.0
            WHEN a.feedback.value IN ('false', 'no') THEN 0.0
            ELSE TRY_CAST(a.feedback.value AS DOUBLE)
        END
    ) AS avg_score,
    COUNT(*) AS sample_count
FROM `${var.catalog}`.`${var.schema}`.mlflow_experiment_trace_metadata
LATERAL VIEW EXPLODE(assessments) AS a
WHERE (DATE(a.last_update_time) BETWEEN :time_range.min AND :time_range.max OR :time_range IS NULL)
  AND a.source.source_type = 'CODE'
GROUP BY 1, 2
ORDER BY score_date, scorer
