-- Data-universe OVERVIEW storage: one row per curated view holding data.
-- Read verbatim by get_data_landscape(), which the agent runtime injects
-- into the system prompt.
--
-- IF NOT EXISTS, deliberately: every deploy re-runs this file, and
-- CREATE OR REPLACE would empty the table until the next profiling run,
-- silently dropping the data universe out of the agent's prompt. The
-- rows belong to jobs/data_universe_profile, which overwrites them.
--
-- No refreshed_at column, also deliberately: this table's contents are
-- rendered into a prompt-cached prefix, so a timestamp would invalidate
-- the cache on every refresh. Run history belongs in the job, not here.

CREATE TABLE IF NOT EXISTS `${var.catalog}`.`${var.schema}`.landscape_views (
  view_name STRING NOT NULL COMMENT 'Unqualified name of the curated view',
  catalog STRING NOT NULL COMMENT 'Catalog holding the view; back-quote it when building a three-part name',
  view_schema STRING NOT NULL COMMENT 'Schema holding the view',
  row_count BIGINT NOT NULL COMMENT 'Row count as of the last profiling run',
  view_description STRING COMMENT 'The view''s Unity Catalog comment; empty string when it has none'
) USING DELTA
COMMENT 'Data-universe OVERVIEW: one row per curated view holding data. Written by the data-universe-profile job, read by get_data_landscape(), injected into the agent system prompt. Empty views are omitted, so a view absent here has no data.';
