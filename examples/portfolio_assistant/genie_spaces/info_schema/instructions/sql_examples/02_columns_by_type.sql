---
question: "What data types are used in system.information_schema.tables?"
usage_guidance: "Use ${var.domain}_catalog_objects for column-level details"
---
SELECT
    data_type,
    COUNT(*) AS column_count
FROM ${var.domain}_catalog_objects
WHERE table_name = 'tables'
GROUP BY data_type
ORDER BY column_count DESC;
