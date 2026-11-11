---
question: "How many tables are in the system information_schema?"
usage_guidance: "Use ${var.domain}_schema_summary for pre-computed counts"
---
SELECT
    table_count
FROM ${var.domain}_schema_summary
WHERE table_catalog = 'system'
  AND table_schema = 'information_schema';
