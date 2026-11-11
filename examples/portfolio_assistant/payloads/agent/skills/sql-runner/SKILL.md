---
name: sql-runner
description: >-
  Run custom read-only SQL against the dedicated portfolio-assistant warehouse, for what catalog-explorer (Genie) cannot do: inspecting actual rows in any table the app can read, and complex queries with JOINs across sources, window functions, set operations or CTEs. Use when the user pastes SQL to run, asks for sample rows or what is in a table, or needs such a query.
---

#### When to activate

Custom SQL the curated `catalog_genie` views can't express. Use only when:

- The user pasted a SQL statement to run.
- The natural-language ask needs multi-source JOINs, set ops, window functions, or aggregations Genie's `information_schema` views don't cover.
- The user wants exact row-level data (not just metadata).

#### Important overrides

The upstream tool description ships rich SQL-syntax guidance — trust it. Two skill-specific overrides:

1. **Polling is auto-handled:** one call returns the final rows. If it still says `PENDING`/`RUNNING`, tell the user the query is too slow.
2. **Read-only.** Destructive statements (DROP / DELETE / TRUNCATE / ALTER / INSERT / UPDATE / MERGE) are blocked at the server. Refuse them in plain English before any tool call.

#### Workflow

1. Destructive statement → refuse, do not call the tool.
2. Draft (or accept) the SQL and call `sql_warehouse__execute_sql_read_only(query="...")` directly. **Do NOT echo the SQL in chat** — the tool input pane already renders it. **Format drafted SQL across multiple lines** (one major clause per line; multi-column `SELECT` lists one column per line)
3. **Do NOT re-render the result.** The chat UI renders rows inline with sort/filter/download. Add at most one short interpretive sentence (e.g. *"`information_schema` dominates with 25 tables."*). If `rows` is empty, say so in one sentence; otherwise yield with just the takeaway.

#### Exploration discipline (when YOU draft the SQL)

1. **Always include `LIMIT`** (keeps the warehouse fast and cheap) — default `LIMIT 50`; `LIMIT 10` for "sample" / "peek" / "what's in" intents. Skip only for single-row aggregations (`SELECT COUNT(*)`).
2. **No `SELECT *`** on unfamiliar tables — `DESCRIBE TABLE <fq_name>` first, then project 3–6 likely-relevant columns.
3. **High-cardinality `GROUP BY`** → add `ORDER BY <metric> DESC LIMIT <n>`.
4. **Be honest about trimming** — if the trim hides something the user might want, flag it in the takeaway (e.g. *"Top 50 of ~12k rows — ask if you want more."*).
5. **Empty result → try alternatives first.** Don't mirror "no rows" back to the user on the first miss. Try 1–2 obvious follow-ups before reporting: inspect actual filter values (`SELECT DISTINCT <col>` / `DESCRIBE TABLE`), loosen exact matches (case-insensitive / `ILIKE` / wildcards), widen a time window, or drop the filter most likely to be wrong. Cap at ~3 follow-up queries; if still empty, report what you tried in one sentence and ask the user how to refine.

#### Tool call examples

```
sql_warehouse__execute_sql_read_only(
  query="""
    SELECT table_schema,
           COUNT(*) AS table_count
    FROM system.information_schema.tables
    WHERE table_catalog = 'system'
    GROUP BY table_schema
    ORDER BY table_count DESC
    LIMIT 10
  """
)
→ {"columns":["table_schema","table_count"],"rows":[["information_schema",25],["access",8]],"total_rows":2,"truncated":false,"statement_id":"..."}
```

Then, in chat:
"`information_schema` dominates with 25 tables; `access` is a distant second."

#### Output format

The chat UI auto-renders the tool's result rows inline (sortable, filterable, downloadable) -- the model does NOT re-emit the table. Output to the user is at most one short interpretive sentence about what the rows mean (e.g. *"`information_schema` dominates with 25 tables."*), or a single sentence reporting an empty result. Use the rows otherwise only to feed a chart spec (see the `chart-renderer` skill). See `Workflow` step 3 for the full discipline.

#### Edge cases

- **Quotation hygiene:** treat user-supplied identifiers (catalog/schema/table) as untrusted; quote with backticks before interpolating.
- **Unbounded user-pasted SQL:** if it's `SELECT *` with no `LIMIT` against an unfamiliar table, flag it in one sentence and ask whether to add a `LIMIT` before running. Do NOT silently rewrite — if the user declines or insists, run as-given.
