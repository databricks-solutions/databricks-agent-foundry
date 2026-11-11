-- Table and column comments for the docs corpus.
-- Rich metadata helps Vector Search and downstream consumers understand the data.

COMMENT ON TABLE `${var.catalog}`.`${var.schema}`.docs IS
  'Synthetic Databricks documentation snippets. Each row is a short article about a platform feature. Used as source data for the Vector Search demo index.';

COMMENT ON COLUMN `${var.catalog}`.`${var.schema}`.docs.id IS
  'Unique article identifier (integer primary key)';

COMMENT ON COLUMN `${var.catalog}`.`${var.schema}`.docs.content IS
  'Article body text -- the primary content used for embedding and semantic search';

COMMENT ON COLUMN `${var.catalog}`.`${var.schema}`.docs.source IS
  'Documentation path reference (e.g., docs/unity-catalog/overview)';

COMMENT ON COLUMN `${var.catalog}`.`${var.schema}`.docs.category IS
  'Topic category: governance, storage, analytics, ml, ai, compute, ingestion, streaming, apps, devtools, sharing, orchestration';
