-- Metric View: catalog_metrics
-- ==============================
-- Unity Catalog Metric View with YAML body. Defines measures
-- and dimensions once; users query with MEASURE() aggregation.
--
-- IMPORTANT limitations:
--   - Cannot mix metric views with regular tables in one Genie Space
--   - Measures must be wrapped in MEASURE() when queried
--   - SELECT * is not supported on metric views
--   - Metric views count toward the 30-table Genie Space limit
--   - The ``source:`` below is double-quoted on purpose.  It needs the same
--     back ticks as the SQL around it so a hyphenated catalog resolves, but
--     the body is parsed as YAML first and a leading back tick cannot start
--     a plain YAML scalar.  Wrapping the whole name in quotes satisfies both.
--
-- Pattern:
--   - source: references a regular view or table
--   - dimensions: per-row attributes for grouping
--   - measures: aggregation expressions (SUM, COUNT, AVG)
--
-- Source view: ${var.catalog}.${var.schema}.catalog_objects (denormalized)
-- Target: ${var.catalog}.${var.schema}.catalog_metrics
--
-- Query example:
--   SELECT `Table Schema`,
--          MEASURE(`Table Count`) AS table_count,
--          MEASURE(`Column Count`) AS column_count
--   FROM ${var.catalog}.${var.schema}.catalog_metrics
--   GROUP BY `Table Schema`

CREATE OR REPLACE VIEW `${var.catalog}`.`${var.schema}`.catalog_metrics
WITH METRICS
LANGUAGE YAML
AS $$
  version: 1.1
  comment: "Catalog explorer KPIs for table and column analysis"
  source: "`${var.catalog}`.`${var.schema}`.catalog_objects"
  dimensions:
    - name: Table Catalog
      expr: table_catalog
    - name: Table Schema
      expr: table_schema
    - name: Table Type
      expr: table_type
    - name: Data Type
      expr: data_type
    - name: Is View
      expr: is_view
  measures:
    - name: Table Count
      expr: COUNT(DISTINCT table_full_name)
    - name: Column Count
      expr: COUNT(column_name)
    - name: Nullable Column Count
      expr: COUNT(CASE WHEN is_nullable = 'YES' THEN 1 END)
$$;
