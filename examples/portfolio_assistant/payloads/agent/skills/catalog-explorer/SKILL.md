---
name: catalog-explorer
description: >-
  Query Unity Catalog metadata (tables, columns, schemas, sizes, data types) through the catalog_genie Genie space, and format byte sizes into readable strings. Use when the user asks how many tables there are, what columns or data types a table has, or how big a table or schema is.
---

#### When to activate

Catalog structure questions — tables, columns, schemas, sizes, data types. Not for documentation lookups — use documentation-explorer instead.

#### Data model

Catalog → Schema → Table → Column. `system` catalog holds Databricks internal metadata.

| Genie View | Grain | Key Columns |
|------------|-------|-------------|
| `catalog_objects` | One row per column per table | `table_catalog`, `table_schema`, `table_name`, `column_name`, `data_type`, `is_nullable`, `table_type`, `is_view` |
| `schema_summary` | One row per schema | `table_catalog`, `table_schema`, `table_count`, `column_count` |

Measures: `table_count` = distinct tables, `column_count` = total columns.

#### How catalog_genie__query_space works

Single-turn SQL agent backed by curated `information_schema` Genie views. Each call is **standalone by default** — pass all required context (catalog/schema names, filters) in the `query` argument.

Conversation continuation is **available** (the upstream tool description names the `conversation_id` argument) but **only use it when the follow-up genuinely depends on the previous turn's intermediate result** (e.g. "now break that down by data type"). Don't pass `conversation_id` just to "save context" on independent questions — it confuses the routing.

#### When to call once vs. decompose

- **One call** for single-fact questions like "how many tables in catalog X", "what columns does Y have", "size of schema Z". Don't pre-decompose these — Genie can answer them directly and extra calls just add latency and chat noise.
- **Multiple calls** only when the user explicitly asks for several distinct facts that don't fit a single SQL aggregation, or when an earlier result is required input to a later question (carry context forward).

When you do decompose: ask one fact per call, use earlier results to inform later queries, synthesize at the end.

#### Workflow

1. Decide whether the question is single-fact (one call) or genuinely multi-fact (decompose).
2. Call `catalog_genie__query_space` with all needed context in the `query` argument.
3. When results include byte counts, pass them through `catalog_functions__format_byte_size`.
4. Synthesize into a concise answer with markdown tables.

#### Tool call examples

Simple (single call):
```
catalog_genie__query_space(question="How many tables are in the system catalog?")
→ "42"
```

Multi-step decomposition — user asks "Compare tables and columns across schemas":
```
catalog_genie__query_space(question="Show table_schema, table_count, and column_count for all schemas in the system catalog")
→ "information_schema: 25 tables, 340 columns | access: 8 tables, 45 columns"
```

Size formatting:
```
catalog_genie__query_space(question="What is the total data size of tables in my_catalog.my_schema?")
→ "1073741824"
catalog_functions__format_byte_size(bytes=1073741824)
→ "1.00 GB"
```

#### Output format

- Lead with a one-sentence summary
- Markdown tables for tabular data; include data types when showing columns
- Exact numbers, never approximations
- No raw SQL unless explicitly asked

#### Edge cases

- **No data:** "The catalog query returned no results. Check catalog/schema names."
- **Ambiguous scope:** Ask which catalog/schema.
- **Row content:** "I can describe table structure but can't query actual data rows."
- **Genie fails:** Report the error and suggest rephrasing.
