# Jobs brick

The Jobs brick deploys Databricks Jobs (Lakeflow Jobs) from version-controlled
`job.yml` files. Use it for recurring or event-driven work that supports an
agent — for example loading documents into a Vector Search source table.

## Instance layout

```text
jobs/
  docs_ingest/
    job.yml
    sql/
      ingest_chunks.sql
    notebooks/
      sync_index.py
```

This is the portfolio example's document-ingestion job: a `sql_task` that
parses and chunks uploaded files with `ai_parse_document` and
`ai_prep_search`, followed by a notebook task that syncs the Vector Search
index. Copy it from `examples/portfolio_assistant/jobs/docs_ingest/`.

Each direct child of `jobs/` is an independent sub-instance. The presence of
`job.yml` opts it into `agent-foundry deploy`.

## Configuration surface

`agent-foundry scaffold jobs` writes the **full Jobs API reference** under
`jobs/example_job/`. That file documents every `JobConfig` field (active or
commented): multi-task DAGs, shared `job_clusters`, serverless
`environments`, compute choices (`environment_key` / `job_cluster_key` /
`existing_cluster_id` / `new_cluster`), schedules and triggers (cron, file
arrival, periodic, table update, continuous), notifications, queue, git
source, `run_as`, and additional task types (wheel, JAR, pipeline, dbt,
`run_job`, `for_each`, condition, dashboard).

Portfolio / example instances intentionally stay small; discoverability lives
in the scaffold + JSON Schema (`job_config.schema.json`), not in the example.

Primary task types with first-class validation:

- `notebook_task`
- `spark_python_task`
- `sql_task` (`query` / `file` / `alert` / `dashboard`)
- `for_each_task` (its nested `task` is validated like a top-level one)

Additional task types are accepted as passthrough payloads and forwarded to
the Jobs API. A job may use at most one of `trigger`, `schedule`, or
`continuous`.

Relative notebook, Spark Python and `sql_task.file` paths beginning with `./`
are uploaded to `/Shared/agent-foundry/<target>/<app>/jobs/<job>/`, mirroring
the folder they sit in locally, and this includes the task nested inside a
`for_each_task`. Two different files that would land on the same workspace
path are rejected rather than silently overwriting each other. Absolute
workspace paths are passed through unchanged.

`dbt_task.project_directory` is the exception: a dbt project is a directory,
not a single file, so a `./`-relative value is rejected at validation. Use a
workspace path or `git_source`.

## Lifecycle and state

Deploy creates the job, resets an existing job tracked by state, or adopts an
existing job with the same name when state is missing. State publishes the
job ID as `${var.job_id__<sub_instance>}`. Destroy requires `--force` because
deleting a job also removes its run history.

```bash
uv run agent-foundry scaffold jobs --instance <app>
uv run agent-foundry brick job validate \
  --instance <app>/jobs/<job> --target dev
uv run agent-foundry brick job deploy \
  --instance <app>/jobs/<job> --target dev
```

Use `--dry-run` on deploy to validate and print the rendered Jobs API
settings without uploading sources or calling the workspace.
