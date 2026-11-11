---
question: "How many tables are in the system information_schema?"
usage_guidance: "Use when the user asks about table counts in the system catalog"
---
SELECT
    COUNT(DISTINCT table_name) AS table_count
FROM system.information_schema.tables
WHERE table_catalog = 'system'
  AND table_schema = 'information_schema';
