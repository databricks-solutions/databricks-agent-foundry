-- Converts a raw byte count into a human-readable string.
-- Useful for catalog exploration where information_schema reports
-- data_size_in_bytes as raw integers.

CREATE OR REPLACE FUNCTION `${var.catalog}`.`${var.agent_schema_name}`.format_byte_size(
  bytes BIGINT COMMENT 'The size in bytes to format (e.g. from information_schema data_size_in_bytes)'
)
RETURNS STRING
COMMENT 'Converts a byte count into a human-readable size string (e.g. 1073741824 becomes 1.00 GB). Use when presenting table or schema sizes to users.'
LANGUAGE SQL
RETURN CASE
  WHEN bytes IS NULL THEN NULL
  WHEN bytes < 1024 THEN CONCAT(CAST(bytes AS STRING), ' B')
  WHEN bytes < 1048576 THEN CONCAT(CAST(ROUND(bytes / 1024.0, 2) AS STRING), ' KB')
  WHEN bytes < 1073741824 THEN CONCAT(CAST(ROUND(bytes / 1048576.0, 2) AS STRING), ' MB')
  WHEN bytes < 1099511627776 THEN CONCAT(CAST(ROUND(bytes / 1073741824.0, 2) AS STRING), ' GB')
  ELSE CONCAT(CAST(ROUND(bytes / 1099511627776.0, 2) AS STRING), ' TB')
END;
