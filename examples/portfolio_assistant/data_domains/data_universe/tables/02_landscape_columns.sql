-- Data-universe DETAIL storage: one row per usable column of a populated
-- view. Read verbatim by get_table_metadata(), which is an agent tool.
--
-- IF NOT EXISTS for the same reason as landscape_views: the rows belong
-- to jobs/data_universe_profile, and replacing the table on every deploy
-- would throw them away.

CREATE TABLE IF NOT EXISTS `${var.catalog}`.`${var.schema}`.landscape_columns (
  view_name STRING NOT NULL COMMENT 'View the column belongs to; matches landscape_views.view_name',
  column_name STRING NOT NULL COMMENT 'Name of the column',
  ordinal_position BIGINT NOT NULL COMMENT 'Position in the view, used to return columns in their declared order',
  data_type STRING COMMENT 'SQL type of the column',
  column_comment STRING COMMENT 'The column''s Unity Catalog comment; empty string when it has none',
  distinct_count BIGINT COMMENT 'Approximate number of distinct values; separates a key from a small dimension',
  filter_values STRING COMMENT 'Exact values of a low-cardinality categorical, joined by " | "; empty when not enumerable'
) USING DELTA
COMMENT 'Data-universe DETAIL: one row per usable column of a populated view. Written by the data-universe-profile job, read by get_table_metadata(). Entirely-NULL columns are omitted, so a column absent here cannot be filtered or joined on.';
