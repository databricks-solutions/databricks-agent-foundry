-- Example: Materialized Delta table with Change Data Feed
-- =========================================================
-- Use tables/ when you need persisted data rather than a computed view.
-- Common reasons:
--   - Vector Search Delta Sync requires a Delta table with CDF enabled
--   - Synthetic or seed data that doesn't derive from existing tables
--   - ETL output that must be materialized for performance
--
-- Multi-statement files are supported. Statements are split on ';' and
-- executed sequentially via the Statement Execution API.
--
-- Variables available: ${var.catalog}, ${var.schema} (from domain.yml)

-- 1. Create the table with Change Data Feed enabled.
--    CDF is required for Delta Sync Vector Search indexes.
CREATE TABLE IF NOT EXISTS `${var.catalog}`.`${var.schema}`.docs (
  id INT,
  content STRING,
  source STRING,
  category STRING,
  doc_uri STRING
) USING DELTA
TBLPROPERTIES (delta.enableChangeDataFeed = true);

-- 2. Idempotent reload: truncate before inserting seed data.
--    Remove this if your table is populated by an upstream pipeline.
TRUNCATE TABLE `${var.catalog}`.`${var.schema}`.docs;

-- 3. Insert seed rows.
INSERT INTO `${var.catalog}`.`${var.schema}`.docs VALUES
(1, 'Unity Catalog provides centralized governance for data and AI assets.', 'docs/unity-catalog', 'governance', '/Volumes/${var.catalog}/${var.schema}/example_sources/unity-catalog.pdf'),
(2, 'Delta Lake brings ACID transactions to data lakes.', 'docs/delta-lake', 'storage', '/Volumes/${var.catalog}/${var.schema}/example_sources/delta-lake.pdf'),
(3, 'Databricks SQL is a serverless data warehouse on the Lakehouse.', 'docs/dbsql', 'analytics', '/Volumes/${var.catalog}/${var.schema}/example_sources/dbsql.pdf');
