-- Source documents referenced by the docs table's doc_uri column.
-- Upload files to /Volumes/${var.catalog}/${var.schema}/docs_sources/.

CREATE VOLUME IF NOT EXISTS `${var.catalog}`.`${var.schema}`.docs_sources;
