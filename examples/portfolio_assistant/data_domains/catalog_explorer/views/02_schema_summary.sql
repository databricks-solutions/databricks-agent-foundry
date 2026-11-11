-- Pre-aggregated view: one row per schema with table/column counts.

CREATE OR REPLACE VIEW `${var.catalog}`.`${var.schema}`.${var.domain}_schema_summary AS
SELECT
    t.table_catalog,
    t.table_schema,
    COUNT(DISTINCT t.table_name) AS table_count,
    SUM(col_counts.column_count) AS total_column_count,
    ROUND(AVG(col_counts.column_count), 1) AS avg_columns_per_table,
    COUNT(DISTINCT CASE WHEN t.table_type = 'VIEW' THEN t.table_name END) AS view_count,
    COUNT(DISTINCT CASE WHEN t.table_type = 'BASE TABLE' THEN t.table_name END) AS base_table_count
FROM system.information_schema.tables t
LEFT JOIN (
    SELECT table_catalog, table_schema, table_name, COUNT(*) AS column_count
    FROM system.information_schema.columns
    GROUP BY 1, 2, 3
) col_counts USING (table_catalog, table_schema, table_name)
WHERE t.table_catalog = 'system'
  AND t.table_schema = 'information_schema'
GROUP BY t.table_catalog, t.table_schema;
