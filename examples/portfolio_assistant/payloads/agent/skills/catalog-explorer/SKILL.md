---
name: catalog-explorer
description: >-
  Answer Unity Catalog metadata questions (tables, columns, schemas, sizes, data types) by asking Genie One about the curated catalog views, and format byte sizes into readable strings. Use when the user asks how many tables there are, what columns or data types a table has, or how big a table or schema is.
---

#### When to activate

Catalog structure questions — tables, columns, schemas, sizes, data types. Not for documentation lookups — use documentation-explorer instead.

#### Data model

Catalog → Schema → Table → Column. `system` catalog holds Databricks internal metadata.

| Curated view | Grain | Key Columns |
|--------------|-------|-------------|
| `catalog_objects` | One row per column per table | `table_catalog`, `table_schema`, `table_name`, `column_name`, `data_type`, `is_nullable`, `table_type`, `is_view` |
| `schema_summary` | One row per schema | `table_catalog`, `table_schema`, `table_count`, `column_count` |

Measures: `table_count` = distinct tables, `column_count` = total columns.

#### How Genie One works

`genie_ask` searches all data the user can read, not one Genie space. **Name the curated view by its full three-part name from the Data Universe** in the question, so Genie queries it instead of searching. A turn takes one to several minutes: poll with `genie_poll_response` until it completes, then fetch rows with `genie_get_query_result` only when you need exact values the answer doesn't state.

Pass `conversation_id` only when the follow-up depends on the previous answer (e.g. "now break that down by data type"); otherwise each question is standalone.

#### When to ask once vs. decompose

- **One question** for single facts like "how many tables in catalog X", "what columns does Y have", "size of schema Z".
- **Several questions** only when the user asks for distinct facts that don't fit one aggregation, or a later question needs an earlier result.

#### Workflow

1. Call `genie_ask` with the view's full name and all filters (catalog, schema) in `question`.
2. Poll `genie_poll_response` until `status` is `completed`.
3. When results include byte counts, pass them through `catalog_functions__format_byte_size`.
4. Answer with a one-sentence summary, exact numbers and the data types when showing columns.

#### Edge cases

- **No data:** "The catalog query returned no results. Check catalog/schema names."
- **Ambiguous scope:** Ask which catalog/schema.
- **Row content:** use sql-runner; Genie answers metadata questions here.
- **Genie fails or is cancelled:** Report the error and suggest rephrasing.
