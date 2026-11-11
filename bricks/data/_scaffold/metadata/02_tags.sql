-- Unity Catalog Tags Template
-- ============================
-- UC tags provide structured metadata for governance and discovery.
-- Unlike COMMENT ON (free text), tags are key-value pairs that can
-- be searched and filtered in the UC UI.
--
-- Common tag patterns:
--   - domain: business domain this asset belongs to
--   - owner: team or person responsible
--   - pii: whether the column contains personally identifiable information
--   - freshness: how often the data is refreshed
--   - source: upstream system of record
--
-- Syntax:
--   ALTER TABLE <table> SET TAGS ('key' = 'value', ...);
--   ALTER TABLE <table> ALTER COLUMN <col> SET TAGS ('key' = 'value');
--
-- Governed tags: if your account has a tag policy on a key (e.g. domain),
-- only its allowed values are accepted. Other values fail with
-- UC_TAG_POLICY_VALUE_NOT_ALLOWED; deploy reports it as a metadata warning
-- and continues. Use an allowed value or a different key.

-- ---- Table-level tags ----

ALTER TABLE `${var.catalog}`.`${var.schema}`.catalog_objects
SET TAGS (
    'domain' = 'catalog_explorer',
    'layer' = 'curated',
    'source' = 'system.information_schema',
    'freshness' = 'real-time'
);

ALTER TABLE `${var.catalog}`.`${var.schema}`.schema_summary
SET TAGS (
    'domain' = 'catalog_explorer',
    'layer' = 'aggregated',
    'source' = 'system.information_schema',
    'freshness' = 'real-time'
);

-- ---- Column-level tags (PII example) ----
-- Uncomment and adapt for columns that contain sensitive data:
--
-- ALTER TABLE `${var.catalog}`.`${var.schema}`.<table>
-- ALTER COLUMN <column> SET TAGS ('pii' = 'true', 'classification' = 'email');
