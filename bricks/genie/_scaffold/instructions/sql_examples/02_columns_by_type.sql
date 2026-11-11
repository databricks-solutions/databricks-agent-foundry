---
question: "What data types are used in system.information_schema.tables?"
usage_guidance: "Use when the user asks about column data types for a specific table"
---
SELECT
    data_type,
    COUNT(*) AS column_count
FROM system.information_schema.columns
WHERE table_catalog = 'system'
  AND table_schema = 'information_schema'
  AND table_name = 'tables'
GROUP BY data_type
ORDER BY column_count DESC;
