# Example Job

`job.yml` is the **full Jobs API configuration reference** for this brick —
not a minimal starter. Every top-level `JobConfig` field appears as an
active key or a `# field:` comment so `agent-foundry scaffold jobs` is how
authors discover clusters, multi-task DAGs, triggers, notifications, and
advanced task types.

Keep one task type per task entry. Relative notebook, Spark Python and
`sql_task.file` sources are uploaded to the workspace when the brick
deploys — including the task nested inside a `for_each_task`. A dbt
project is a directory rather than a single file, so
`dbt_task.project_directory` must be a workspace path or come from
`git_source`.

The active example uses a paused file-arrival trigger plus a serverless
`environments` block. Uncomment `schedule`, `continuous`, `job_clusters`,
or other sections as needed; do not enable more than one of
`trigger` / `schedule` / `continuous`.

For a working document-ingestion job (a SQL file that parses and chunks
uploaded files into a Vector Search source table, then a notebook that
syncs the index), copy `examples/portfolio_assistant/jobs/docs_ingest/`.
