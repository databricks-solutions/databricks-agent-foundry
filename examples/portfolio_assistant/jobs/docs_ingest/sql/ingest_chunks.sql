-- Parse and chunk the documents in the docs_sources volume into docs_chunks.
--
-- Parameters (sql_task.parameters in ../job.yml):
--   :volume_path   /Volumes/<catalog>/<schema>/docs_sources/  (trailing slash)
--   :chunks_table  <catalog>.<schema>.docs_chunks
--
-- PDF, Office and image files go through ai_parse_document; .md and .txt are
-- chunked as plain text. ai_prep_search splits both into search-ready chunks
-- and adds title, section and page context to chunk_to_embed.
-- Requires a SQL warehouse that supports ai_parse_document and ai_prep_search.
--
-- Only files that are new or modified since their last ingestion are parsed.
-- Seed rows (chunk_id 'seed:...') are never deleted.

-- 1. Upsert the chunks of new and modified files.
MERGE INTO IDENTIFIER(:chunks_table) AS t
USING (
  WITH files AS (
    SELECT
      regexp_replace(path, '^dbfs:', '') AS doc_uri,
      modificationTime AS modified_at,
      content
    FROM READ_FILES(:volume_path, format => 'binaryFile')
  ),
  ingested AS (
    SELECT doc_uri, max(source_modified_at) AS modified_at
    FROM IDENTIFIER(:chunks_table)
    GROUP BY doc_uri
  ),
  changed AS (
    SELECT f.doc_uri, f.modified_at, f.content
    FROM files AS f
    LEFT JOIN ingested AS i ON f.doc_uri = i.doc_uri
    WHERE i.modified_at IS NULL OR f.modified_at > i.modified_at
  ),
  prepped AS (
    SELECT doc_uri, modified_at, ai_prep_search(ai_parse_document(content, map('version', '2.0'))) AS result
    FROM changed
    WHERE lower(doc_uri) RLIKE '[.](pdf|docx?|pptx?|jpe?g|png|tiff?)$'
    UNION ALL
    SELECT doc_uri, modified_at, ai_prep_search(decode(content, 'UTF-8')) AS result
    FROM changed
    WHERE lower(doc_uri) RLIKE '[.](md|txt)$'
  )
  SELECT
    concat(p.doc_uri, '#', chunk.value:chunk_position::INT) AS chunk_id,
    p.doc_uri,
    substr(p.doc_uri, length(:volume_path) + 1) AS source,
    chunk.value:chunk_position::INT AS chunk_position,
    chunk.value:chunk_to_retrieve::STRING AS content,
    chunk.value:chunk_to_embed::STRING AS chunk_to_embed,
    chunk.value:pages[0].page_id::INT + 1 AS page_number,
    chunk.value:pages[0].image_uri::STRING AS image_uri,
    'uploaded' AS category,
    p.modified_at AS source_modified_at
  FROM prepped AS p,
    LATERAL variant_explode(p.result:document.contents) AS chunk
) AS s
ON t.chunk_id = s.chunk_id
WHEN MATCHED THEN UPDATE SET *
WHEN NOT MATCHED THEN INSERT *;

-- 2. Drop chunks left over from an older version of a re-ingested file.
DELETE FROM IDENTIFIER(:chunks_table) AS old
WHERE old.chunk_id NOT LIKE 'seed:%'
  AND EXISTS (
    SELECT 1
    FROM IDENTIFIER(:chunks_table) AS newer
    WHERE newer.doc_uri = old.doc_uri
      AND newer.source_modified_at > old.source_modified_at
  );

-- 3. Drop chunks of files that are no longer in the volume.
DELETE FROM IDENTIFIER(:chunks_table)
WHERE chunk_id NOT LIKE 'seed:%'
  AND doc_uri NOT IN (
    SELECT regexp_replace(path, '^dbfs:', '')
    FROM READ_FILES(:volume_path, format => 'binaryFile')
  );
