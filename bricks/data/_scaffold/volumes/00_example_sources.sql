-- Unity Catalog volume for source documents referenced by indexed rows.
-- Store citation URIs in the source Delta table using paths such as:
-- /Volumes/${var.catalog}/${var.schema}/example_sources/document.pdf

CREATE VOLUME IF NOT EXISTS `${var.catalog}`.`${var.schema}`.example_sources;
