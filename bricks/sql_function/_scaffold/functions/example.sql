-- Example SQL function deployed by the sql_function brick.
-- Replace with your own function definitions.
--
-- IMPORTANT: Always include COMMENT on the function AND each parameter.
-- The agent LLM uses these comments to understand when and how to call
-- the function.  Missing comments degrade tool-calling accuracy.
--
-- Address the agent schema as `${var.catalog}`.`${var.agent_schema_name}`, not
-- ${var.agent_schema}: the latter is two-part and cannot be back-quoted, so a
-- hyphen in the catalog name breaks the identifier.

CREATE OR REPLACE FUNCTION `${var.catalog}`.`${var.agent_schema_name}`.example_fn(
  input STRING COMMENT 'The text to convert to uppercase'
)
RETURNS STRING
COMMENT 'Converts input text to uppercase. Use for text normalization.'
LANGUAGE SQL
RETURN UPPER(input);
