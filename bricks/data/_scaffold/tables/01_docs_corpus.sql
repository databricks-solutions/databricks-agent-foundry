-- Example: Materialized Delta table with Change Data Feed
-- =========================================================
-- Use tables/ when you need persisted data rather than a computed view.
-- Common reasons:
--   - Vector Search Delta Sync requires a Delta table with CDF enabled
--   - Synthetic or seed data that doesn't derive from existing tables
--   - ETL output that must be materialized for performance
--
-- This example is shaped as a document-chunk table: one row per chunk, the
-- layout written by an ai_parse_document + ai_prep_search ingestion job (see
-- examples/portfolio_assistant/jobs/docs_ingest). Index it with
-- primary_key chunk_id and embedding source column chunk_to_embed.
--
-- Multi-statement files are supported. Statements are split on ';' and
-- executed sequentially via the Statement Execution API.
--
-- Variables available: ${var.catalog}, ${var.schema} (from domain.yml)

-- 1. Create the table with Change Data Feed enabled.
--    CDF is required for Delta Sync Vector Search indexes.
--    IF NOT EXISTS (not OR REPLACE) so redeploys keep ingested rows.
CREATE TABLE IF NOT EXISTS `${var.catalog}`.`${var.schema}`.docs (
  chunk_id STRING,
  doc_uri STRING,
  source STRING,
  chunk_position INT,
  content STRING,
  chunk_to_embed STRING,
  page_number INT,
  image_uri STRING,
  category STRING,
  source_modified_at TIMESTAMP
) USING DELTA
TBLPROPERTIES (delta.enableChangeDataFeed = true);

-- 2. Idempotent seed rows. Remove this if an ingestion job fills the table.
MERGE INTO `${var.catalog}`.`${var.schema}`.docs AS t
USING (
  SELECT * FROM VALUES
    ('Unity Catalog provides centralized governance for data and AI assets.', 'unity-catalog.pdf', 'governance'),
    ('Delta Lake brings ACID transactions to data lakes.', 'delta-lake.pdf', 'storage'),
    ('Databricks SQL is a serverless data warehouse on the Lakehouse.', 'dbsql.pdf', 'analytics')
    AS seed(content, source, category)
) AS s
ON t.chunk_id = concat('seed:', s.source)
WHEN MATCHED THEN UPDATE SET
  t.content = s.content,
  t.chunk_to_embed = s.content,
  t.category = s.category
WHEN NOT MATCHED THEN INSERT
  (chunk_id, doc_uri, source, chunk_position, content, chunk_to_embed, page_number, category)
VALUES (
  concat('seed:', s.source),
  concat('/Volumes/${var.catalog}/${var.schema}/example_sources/', s.source),
  s.source,
  0,
  s.content,
  s.content,
  1,
  s.category
);
