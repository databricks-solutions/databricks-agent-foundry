---
name: sql-runner
description: Runs read-only SQL with the sql_warehouse__execute_sql_read_only tool and keeps results small. Use whenever you query tables directly, when the user pastes SQL to run, or when a question needs exact rows, JOINs, window functions or aggregations.
---
# SQL runner

#### When to use

- The user pasted a SQL statement to run.
- The question needs exact row-level data, multi-table JOINs, set operations,
  window functions or aggregations.
- For broad natural-language questions about data, prefer Genie; use SQL when
  you know the table and the columns.

#### Read-only

Only run `SELECT`, `DESCRIBE`, `SHOW` and `WITH ... SELECT`. Refuse DROP,
DELETE, TRUNCATE, ALTER, INSERT, UPDATE, MERGE, CREATE and GRANT in plain
English before any tool call.

#### Workflow

1. Draft (or accept) the SQL and call `sql_warehouse__execute_sql_read_only(query="...")`. Use
   fully-qualified names (`catalog.schema.table`). Format drafted SQL across
   multiple lines, one major clause per line. Do NOT echo the SQL in chat; the
   tool input pane already shows it.
2. If the result has a `statement_id` and its state is `PENDING` or `RUNNING`,
   call `sql_warehouse__poll_sql_result(statement_id="...")`. Poll at most 3 times; if it is
   still running, tell the user the query is too slow and offer to narrow it.
3. Do NOT re-render the result. The chat UI shows the rows with sort, filter and
   download. Add at most one short sentence about what the rows mean. If `rows`
   is empty, follow the empty-result rule below.

#### Exploration discipline (when you draft the SQL)

1. **Always include `LIMIT`** (keeps the warehouse fast and cheap): `LIMIT 50` by default, `LIMIT 10` for "sample",
   "peek" or "what's in". Skip it only for single-row aggregations
   (`SELECT COUNT(*)`).
2. **No `SELECT *` on unfamiliar tables**: run `DESCRIBE TABLE <fq_name>` first,
   then select the 3 to 6 relevant columns.
3. **High-cardinality `GROUP BY`**: add `ORDER BY <metric> DESC LIMIT <n>`.
4. **Aggregate in SQL, not in the chat**: compute counts, sums and averages in
   the query instead of fetching rows and summarising them yourself.
5. **Be honest about trimming**: if a limit hides something the user may want,
   say so (e.g. *"Top 50 of ~12k rows; ask if you want more."*).
6. **Empty result: try alternatives first.** Check the actual filter values
   (`SELECT DISTINCT <col> ... LIMIT 20`), loosen exact matches (`ILIKE`),
   widen a time window, or drop the filter most likely to be wrong. Stop after
   about 3 follow-up queries, report what you tried in one sentence and ask
   how to refine.

#### Example

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
```

#### Edge cases

- **Identifiers from the user** are untrusted: quote catalog, schema and table
  names with backticks before putting them into SQL.
- **Unbounded pasted SQL**: if it is `SELECT *` without `LIMIT` on an
  unfamiliar table, say so in one sentence and ask whether to add a `LIMIT`.
  Do not rewrite it silently; if the user insists, run it as given.
