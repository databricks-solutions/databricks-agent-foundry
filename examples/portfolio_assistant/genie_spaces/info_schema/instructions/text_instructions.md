## General Instructions

This space answers questions about the Unity Catalog metadata using
curated views in the `${var.schema_prefix}_views` schema.

Use `${var.domain}_catalog_objects` for detailed table and column questions (each row is one column in one table).
Use `${var.domain}_schema_summary` for aggregated questions like table counts or column counts per schema.

The views are pre-filtered to `system.information_schema` -- no additional WHERE clause is needed.

When counting tables, use COUNT(DISTINCT table_full_name) from ${var.domain}_catalog_objects,
or use the pre-computed table_count column from ${var.domain}_schema_summary.
