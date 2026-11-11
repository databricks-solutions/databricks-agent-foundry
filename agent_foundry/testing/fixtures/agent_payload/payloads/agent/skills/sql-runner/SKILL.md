---
name: sql-runner
description: >-
  Run read-only SQL on the app's SQL warehouse. Use when the user asks to run a query, look at rows in a table, or check something with SQL.
---

#### When to activate

The user asks to run SQL, inspect rows, or check a fact with a query.

#### Workflow

1. Refuse destructive statements (DROP, DELETE, TRUNCATE, ALTER, INSERT,
   UPDATE, MERGE) before any tool call.
2. Call `sql_warehouse__execute_sql_read_only(query="...")` once. Polling is
   handled for you; there is no separate poll tool.
3. Add a `LIMIT 20` unless the user asks for more rows.

#### Tool call examples

- "Who am I in SQL?" → `sql_warehouse__execute_sql_read_only(query="SELECT current_user()")`
- "What is today's date?" → `sql_warehouse__execute_sql_read_only(query="SELECT current_date()")`

#### Output format

Do not repeat the rows in prose; the chat renders them. Add at most one
sentence with the takeaway.

#### Edge cases

- Empty result: say so in one sentence.
- Permission error: name the table and say the app cannot read it.
- Still `PENDING` or `RUNNING`: the query is too slow; say so instead of retrying.
