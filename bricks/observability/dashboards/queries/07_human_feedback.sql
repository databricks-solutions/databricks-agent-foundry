-- Daily human feedback (thumbs up/down) from chat UI.
-- Uses :time_range.min / :time_range.max DATE RANGE parameter (default: last 30 days).
-- Unpivots positive/negative into rows so Lakeview can group by sentiment.
SELECT
    feedback_date,
    sentiment,
    feedback_count
FROM (
    SELECT
        DATE_TRUNC('DAY', a.last_update_time) AS feedback_date,
        SUM(CASE WHEN a.feedback.value IN ('true', 'yes', 'thumbs_up', '1') THEN 1 ELSE 0 END) AS Positive,
        SUM(CASE WHEN a.feedback.value IN ('false', 'no', 'thumbs_down', '0') THEN 1 ELSE 0 END) AS Negative
    FROM `${var.catalog}`.`${var.schema}`.mlflow_experiment_trace_metadata
    LATERAL VIEW EXPLODE(assessments) AS a
    WHERE (DATE(a.last_update_time) BETWEEN :time_range.min AND :time_range.max OR :time_range IS NULL)
      AND a.source.source_type = 'HUMAN'
    GROUP BY 1
)
UNPIVOT (
    feedback_count FOR sentiment IN (Positive, Negative)
)
ORDER BY feedback_date, sentiment
