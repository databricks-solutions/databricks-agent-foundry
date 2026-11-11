---
name: chart-renderer
description: >-
  Render bar, line, area, pie and scatter charts inline in chat by emitting an `echarts-chart` JSON spec, wide-format only (aggregate and pivot in SQL first). Use whenever the user asks for a chart, plot, graph or visualization, or right after another skill returns about 10 or more rows with a clear time or category axis that a chart would make easier to scan.
---

#### Output contract

Emit a fenced markdown block with language tag `echarts-chart`
containing a JSON spec.  The chat UI parses the spec and renders an
interactive chart inline.  This is the **ONLY** way to render charts --
never produce ASCII-art charts, matplotlib screenshots, or links.

Spec schema (wide-format only):

```json
{
  "type": "bar | line | area | pie | scatter",
  "title?": "string -- chart title shown above the plot",
  "data": [{ "<field>": "<value>", ... }, ...],
  "x": "string -- field for the x-axis (or pie slice label)",
  "y": ["string", ...] -- one field per series; multiple => multi-series,
  "palette?": ["#hex", ...]
}
```

Critical: the renderer is wide-format only.  One row per `x` value, one
column per series listed in `y`.  There is no `groupBy`, no aggregation,
no log axis -- those are deliberately not in the schema.

#### Procedure

1. Fetch the underlying rows via the appropriate skill (`sql-runner`,
   `catalog-explorer`, etc.).  If the data is already in the
   conversation, reuse it; do not re-query.
2. **Aggregate and pivot in SQL** so the result is already wide-format
   before you emit the spec.  Do NOT pass long-format rows and expect
   the chart to bucket them.
3. **Cap data at 500 rows** (one row per x-axis value). The whole spec
   is inlined into the chat as JSON, so every row costs LLM tokens to
   write and read back. Aim for **≤ 100 rows** when you can. If the raw
   result is larger, **reduce it before emitting** using one of:
   - **Coarser bucket** — re-aggregate by week / month / quarter
     instead of day; by region instead of zip code. Re-run the source
     query with the new `date_trunc` / `GROUP BY` if needed.
   - **Top-N + "Other"** — keep the top N (e.g. 10) categories and
     fold the long tail into a single `"Other"` row whose value is the
     sum of the rest.
   - **Narrow the window** — last 90 days instead of 5 years; one
     account instead of all accounts.
   - **Filter to what was asked** — drop dimensions the user didn't
     mention; charts answer one question at a time.
4. **If you cannot get under 500 rows without dropping information the
   user explicitly asked for, do NOT emit a chart.** Tell the user the
   raw set is too large (give the row count) and ask how to reduce it
   ("Bucket by week instead of day?" / "Top 20 only?" / "Limit to
   region X?"). Wait for their answer before retrying.
5. Emit the fence as the FIRST or LAST element of your response, on its
   own paragraph.  Never wrap it in another code block.
6. Add a one-sentence summary above the fence ("Here's a bar chart of
   requests per day:").  Do not describe the chart contents in prose
   afterward -- the chart is its own narration.

#### Single-series example (one `y` field)

Source query:
```sql
SELECT date_trunc('day', request_time) AS date, count(*) AS count
FROM requests
GROUP BY 1 ORDER BY 1;
```

Resulting fence:
```echarts-chart
{"type":"bar","title":"Requests per day","x":"date","y":["count"],"data":[{"date":"2026-05-03","count":17},{"date":"2026-05-04","count":5},{"date":"2026-05-05","count":55}]}
```

#### Multi-series example (pivot in SQL)

When you want one line/bar group per category (e.g. DBUs per month
broken down by product), pivot the data in SQL using `GROUP BY` plus
`SUM(CASE WHEN ...)` (or `PIVOT`) so each row already carries one
column per series.

Source query:
```sql
SELECT
  date_trunc('month', usage_date) AS month,
  sum(case when product = 'JOBS'          then dbus end) AS jobs,
  sum(case when product = 'MODEL_SERVING' then dbus end) AS model_serving,
  sum(case when product = 'VECTOR_SEARCH' then dbus end) AS vector_search
FROM usage
GROUP BY 1 ORDER BY 1;
```

Resulting fence (each `y` entry is a series; missing cells are `null`):
```echarts-chart
{"type":"bar","title":"DBUs per month by product","x":"month","y":["jobs","model_serving","vector_search"],"data":[{"month":"2026-03","jobs":410.1,"model_serving":124.66,"vector_search":866.06},{"month":"2026-04","jobs":null,"model_serving":null,"vector_search":2179.33}]}
```

#### Notes

- Use `pie` only for a single series (`y` length 1) and ≤10 slices.
- `scatter` requires numeric `x`; `bar`/`line`/`area` accept either.
- If your raw data spans many orders of magnitude (one series dominates),
  prefer separating the dominant series into its own chart rather than
  asking for a log axis -- log axes are not supported by this renderer.
