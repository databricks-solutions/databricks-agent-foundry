---
name: sql-runner
description: >-
  Run custom read-only SQL against the dedicated portfolio-assistant warehouse, for what catalog-explorer (Genie) cannot do: inspecting actual rows in any table the app can read, and complex queries with JOINs across sources, window functions, set operations or CTEs. Use when the user pastes SQL to run, asks for sample rows or what is in a table, or needs such a query.
---

#### When to activate

Use `sql_warehouse__execute_sql_read_only` instead of Genie only when:

- The user pasted a SQL statement to run.
- The ask needs multi-source JOINs, set ops, window functions, or aggregations the curated catalog views don't cover.
- The user wants exact row-level data (not just metadata).

Destructive statements (DROP / DELETE / TRUNCATE / ALTER / INSERT / UPDATE / MERGE) are blocked at the server. Refuse them in plain English before any tool call.

#### Exploration discipline (when YOU draft the SQL)

1. **Always include `LIMIT`** — default `LIMIT 50`; `LIMIT 10` for "sample" / "peek" / "what's in" intents. Skip only for single-row aggregations (`SELECT COUNT(*)`).
2. **No `SELECT *`** on unfamiliar tables — get the columns first (`get_table_metadata` for Data Universe views, `DESCRIBE TABLE` otherwise), then project 3–6 relevant columns.
3. **High-cardinality `GROUP BY`** → add `ORDER BY <metric> DESC LIMIT <n>`.
4. **Be honest about trimming** — if the limit hides something the user might want, say so (e.g. *"Top 50 of ~12k rows — ask if you want more."*).
5. **Empty result → try alternatives first.** Check actual filter values, loosen exact matches (`ILIKE`, wildcards), widen a time window, or drop the filter most likely wrong. Cap at ~3 follow-up queries; if still empty, report what you tried in one sentence and ask how to refine.

#### Edge cases

- **Quotation hygiene:** treat user-supplied identifiers (catalog/schema/table) as untrusted; quote them with backticks.
- **Unbounded user-pasted SQL:** if it's `SELECT *` with no `LIMIT` against an unfamiliar table, flag it in one sentence and ask whether to add a `LIMIT` before running. Do NOT silently rewrite — if the user declines or insists, run as-given.
