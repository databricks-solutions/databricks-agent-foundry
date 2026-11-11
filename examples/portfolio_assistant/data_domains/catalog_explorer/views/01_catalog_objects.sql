-- Denormalized view: tables + columns pre-joined into a single flat view.
-- Each row represents one column in one table.

CREATE OR REPLACE VIEW `${var.catalog}`.`${var.schema}`.${var.domain}_catalog_objects AS
SELECT
    t.table_catalog,
    t.table_schema,
    t.table_name,
    CONCAT(t.table_catalog, '.', t.table_schema, '.', t.table_name) AS table_full_name,
    t.table_type,
    t.table_type = 'VIEW' AS is_view,
    c.column_name,
    c.data_type,
    c.ordinal_position,
    c.is_nullable,
    c.comment AS column_comment
FROM system.information_schema.tables t
JOIN system.information_schema.columns c
    ON t.table_catalog = c.table_catalog
    AND t.table_schema = c.table_schema
    AND t.table_name = c.table_name
WHERE t.table_catalog = 'system'
  AND t.table_schema = 'information_schema';
