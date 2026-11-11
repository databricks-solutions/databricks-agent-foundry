-- Data-universe OVERVIEW (tier 1 of 2). One row per curated view that
-- actually holds data: what exists, where it lives, how big it is and
-- what it means. Facts only -- which view answers which question is the
-- agent's skills to decide, not this function's.
--
-- The runtime reads this at request assembly and renders it into the
-- system prompt, so the agent never spends a tool call to discover the
-- catalog. That has two consequences worth knowing before editing:
--
--   * The projection below IS the injected prompt block. Adding a column
--     here adds a column to every request's context; the runtime renders
--     whatever comes back, so nothing else has to change.
--   * The output must be STABLE between refreshes, because it sits in
--     the prompt-cached prefix. Never add a timestamp. `row_count` is
--     already live enough to cost one cache write per profiling run --
--     if that ever matters, bucket it to an order of magnitude here.
--
-- Empty views are omitted upstream by the data_universe_profile job, so
-- a view missing from this list has no data. The ORDER BY is what makes
-- the rendered block byte-stable across runs.

CREATE OR REPLACE FUNCTION `${var.catalog}`.`${var.agent_schema_name}`.get_data_landscape()
RETURNS TABLE (
  view_name STRING,
  catalog STRING,
  view_schema STRING,
  row_count BIGINT,
  view_description STRING
)
COMMENT 'Data-universe OVERVIEW: one row per curated view holding data. catalog + view_schema give the three-part `catalog`.`schema`.`view` location to query; row_count is the row count as of the last profiling run; view_description is the view''s Unity Catalog comment. Empty views are omitted, so a view absent from this list has no data. Injected into the agent system prompt rather than called as a tool -- use get_table_metadata(view_name) for a specific view''s columns and filter values.'
RETURN
  SELECT view_name, catalog, view_schema, row_count, view_description
  FROM `${var.catalog}`.`${var.agent_schema_name}`.landscape_views
  ORDER BY view_name;
