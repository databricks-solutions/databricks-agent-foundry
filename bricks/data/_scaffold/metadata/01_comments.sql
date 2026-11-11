-- Table & Column Metadata: COMMENT ON
-- =====================================
-- UC metadata is the HIGHEST priority configuration for Genie accuracy
-- (see field guide section 4.7 - Configuration Priority Framework).
--
-- Rules:
--   - Every table needs a COMMENT ON TABLE describing what it contains
--   - Every column needs a COMMENT ON COLUMN with business meaning
--   - Include units, format, and time range where applicable
--   - Use plain language a business user would understand
--
-- Target: ${var.catalog}.${var.schema} views

-- ---- catalog_objects (denormalized view) ----

COMMENT ON TABLE `${var.catalog}`.`${var.schema}`.catalog_objects
IS 'Flat view of all tables and their columns in the catalog, pre-joined for easy querying. Each row represents one column in one table.';

COMMENT ON COLUMN `${var.catalog}`.`${var.schema}`.catalog_objects.table_catalog
IS 'The Unity Catalog catalog name containing the table';

COMMENT ON COLUMN `${var.catalog}`.`${var.schema}`.catalog_objects.table_schema
IS 'The schema (database) name containing the table';

COMMENT ON COLUMN `${var.catalog}`.`${var.schema}`.catalog_objects.table_name
IS 'The name of the table or view';

COMMENT ON COLUMN `${var.catalog}`.`${var.schema}`.catalog_objects.table_full_name
IS 'Fully qualified three-part name: catalog.schema.table';

COMMENT ON COLUMN `${var.catalog}`.`${var.schema}`.catalog_objects.table_type
IS 'Type of object: BASE TABLE, VIEW, or MANAGED';

COMMENT ON COLUMN `${var.catalog}`.`${var.schema}`.catalog_objects.is_view
IS 'True if the object is a VIEW, false for base tables';

COMMENT ON COLUMN `${var.catalog}`.`${var.schema}`.catalog_objects.column_name
IS 'Name of the column within the table';

COMMENT ON COLUMN `${var.catalog}`.`${var.schema}`.catalog_objects.data_type
IS 'SQL data type of the column (STRING, INT, TIMESTAMP, etc.)';

COMMENT ON COLUMN `${var.catalog}`.`${var.schema}`.catalog_objects.ordinal_position
IS 'Column position within the table (1-based)';

COMMENT ON COLUMN `${var.catalog}`.`${var.schema}`.catalog_objects.is_nullable
IS 'Whether the column allows NULL values (YES or NO)';

COMMENT ON COLUMN `${var.catalog}`.`${var.schema}`.catalog_objects.column_comment
IS 'User-defined description of the column from Unity Catalog';

-- ---- schema_summary (pre-aggregated view) ----

COMMENT ON TABLE `${var.catalog}`.`${var.schema}`.schema_summary
IS 'Aggregated statistics per schema: table counts, column counts, and type breakdowns. One row per schema.';

COMMENT ON COLUMN `${var.catalog}`.`${var.schema}`.schema_summary.table_catalog
IS 'The Unity Catalog catalog name';

COMMENT ON COLUMN `${var.catalog}`.`${var.schema}`.schema_summary.table_schema
IS 'The schema name';

COMMENT ON COLUMN `${var.catalog}`.`${var.schema}`.schema_summary.table_count
IS 'Total number of tables and views in the schema';

COMMENT ON COLUMN `${var.catalog}`.`${var.schema}`.schema_summary.total_column_count
IS 'Total number of columns across all tables in the schema';

COMMENT ON COLUMN `${var.catalog}`.`${var.schema}`.schema_summary.avg_columns_per_table
IS 'Average number of columns per table, rounded to 1 decimal';

COMMENT ON COLUMN `${var.catalog}`.`${var.schema}`.schema_summary.view_count
IS 'Number of views in the schema';

COMMENT ON COLUMN `${var.catalog}`.`${var.schema}`.schema_summary.base_table_count
IS 'Number of base (non-view) tables in the schema';
