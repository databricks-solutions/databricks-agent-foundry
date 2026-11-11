-- Data-universe DETAIL (tier 2 of 2). Given a view_name from the
-- overview, returns one row per usable column of that view.
--
-- This one IS a tool. It stays on demand rather than being injected
-- because it is per-view and would grow the prompt without bound; a
-- request typically needs it for one or two views.
--
-- It replaces the DESCRIBE / SELECT DISTINCT / COUNT(DISTINCT) probing
-- an agent otherwise does before it can write a correct query:
--
--   distinct_count  separates a key from a small dimension, and warns
--                   when a GROUP BY is about to explode.
--   filter_values   the exact values of a low-cardinality categorical.
--                   Filter on these verbatim -- guessing a label is the
--                   usual cause of a confidently empty result.
--
-- Columns that are entirely NULL are omitted upstream: they cannot be
-- filtered or joined on, so a column absent here carries no data.

CREATE OR REPLACE FUNCTION `${var.catalog}`.`${var.agent_schema_name}`.get_table_metadata(
  p_view_name STRING COMMENT 'A view_name from the data universe in the system prompt, unqualified (e.g. catalog_objects)'
)
RETURNS TABLE (
  column_name STRING,
  data_type STRING,
  column_comment STRING,
  distinct_count BIGINT,
  filter_values STRING
)
COMMENT 'Data-universe DETAIL: one row per usable column of the view named by p_view_name (take the name from the data universe in the system prompt). data_type and column_comment describe the column; distinct_count is the approximate cardinality, so use it instead of COUNT(DISTINCT); filter_values lists the exact values of a low-cardinality categorical as a " | "-joined string ('''' otherwise) -- filter on those verbatim rather than guessing. Entirely-NULL columns are omitted, so a column absent here cannot be used. Call this before querying a view instead of DESCRIBE or SELECT DISTINCT.'
RETURN
  SELECT column_name, data_type, column_comment, distinct_count, filter_values
  FROM `${var.catalog}`.`${var.agent_schema_name}`.landscape_columns
  WHERE view_name = p_view_name
  ORDER BY ordinal_position;
