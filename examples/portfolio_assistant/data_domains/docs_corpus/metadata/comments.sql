-- Table and column comments for the docs corpus.
-- Rich metadata helps Vector Search and downstream consumers understand the data.

COMMENT ON TABLE `${var.catalog}`.`${var.schema}`.docs_chunks IS
  'Documentation chunks for the Vector Search demo index. Filled by the docs_ingest job from files in the docs_sources volume, plus synthetic seed articles.';

COMMENT ON COLUMN `${var.catalog}`.`${var.schema}`.docs_chunks.chunk_id IS
  'Primary key: <doc_uri>#<chunk_position> for ingested chunks, seed:<source> for seed articles';

COMMENT ON COLUMN `${var.catalog}`.`${var.schema}`.docs_chunks.doc_uri IS
  'Source file in the docs_sources volume, used for citations';

COMMENT ON COLUMN `${var.catalog}`.`${var.schema}`.docs_chunks.source IS
  'File path relative to the docs_sources volume (seed articles: docs/<topic>/overview)';

COMMENT ON COLUMN `${var.catalog}`.`${var.schema}`.docs_chunks.chunk_position IS
  '0-based position of the chunk within its document';

COMMENT ON COLUMN `${var.catalog}`.`${var.schema}`.docs_chunks.content IS
  'Raw chunk text returned to the agent';

COMMENT ON COLUMN `${var.catalog}`.`${var.schema}`.docs_chunks.chunk_to_embed IS
  'Chunk text enriched with document title, section and page context; the embedding source column';

COMMENT ON COLUMN `${var.catalog}`.`${var.schema}`.docs_chunks.page_number IS
  '1-based page the chunk starts on; NULL for text and markdown files';

COMMENT ON COLUMN `${var.catalog}`.`${var.schema}`.docs_chunks.image_uri IS
  'Rendered page image, populated only when page images are enabled in the ingestion job';

COMMENT ON COLUMN `${var.catalog}`.`${var.schema}`.docs_chunks.category IS
  'Topic category for seed articles; uploaded for ingested files';

COMMENT ON COLUMN `${var.catalog}`.`${var.schema}`.docs_chunks.source_modified_at IS
  'Modification time of the source file when it was ingested; used to skip unchanged files';
