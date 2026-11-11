"""Typed contract for ``instances/<inst>/jobs/<name>/job.yml``.

Mirrors the Databricks Jobs / Lakeflow Jobs API surface that the Jobs
brick can author and deploy. Nested cluster / library / advanced task
payloads that would otherwise require re-modeling the full SDK are kept
as open ``dict`` objects so scaffold examples remain valid and
``JobSettings.from_dict`` can pass them through unchanged.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PauseStatus = Literal["PAUSED", "UNPAUSED"]
RunIf = Literal[
    "ALL_SUCCESS",
    "AT_LEAST_ONE_SUCCESS",
    "NONE_FAILED",
    "ALL_DONE",
    "AT_LEAST_ONE_FAILED",
    "ALL_FAILED",
]


class NotebookTaskConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    notebook_path: str
    base_parameters: dict[str, str] = Field(default_factory=dict)
    source: Literal["WORKSPACE", "GIT"] | None = None
    warehouse_id: str | None = None


class SparkPythonTaskConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    python_file: str
    parameters: list[str] = Field(default_factory=list)
    source: Literal["WORKSPACE", "GIT"] | None = None


class SqlQueryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query_id: str


class SqlFileConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    source: Literal["WORKSPACE", "GIT"] | None = None


class SqlAlertConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alert_id: str
    pause_subscriptions: bool | None = None


class SqlDashboardConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dashboard_id: str
    subscriptions: list[dict[str, Any]] = Field(default_factory=list)
    custom_subject: str | None = None
    pause_subscriptions: bool | None = None


class SqlTaskConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    warehouse_id: str
    query: SqlQueryConfig | None = None
    file: SqlFileConfig | None = None
    alert: SqlAlertConfig | None = None
    dashboard: SqlDashboardConfig | None = None
    parameters: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _exactly_one_sql_payload(self) -> "SqlTaskConfig":
        configured = [
            self.query is not None,
            self.file is not None,
            self.alert is not None,
            self.dashboard is not None,
        ]
        if sum(configured) != 1:
            raise ValueError("exactly one of query, file, alert, or dashboard is required on sql_task")
        return self


class TaskDependency(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_key: str
    outcome: str | None = None


class EmailNotificationsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    on_start: list[str] = Field(default_factory=list)
    on_success: list[str] = Field(default_factory=list)
    on_failure: list[str] = Field(default_factory=list)
    on_duration_warning_threshold_exceeded: list[str] = Field(default_factory=list)
    on_streaming_backlog_exceeded: list[str] = Field(default_factory=list)
    no_alert_for_skipped_runs: bool | None = None


class WebhookNotificationsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    on_start: list[dict[str, str]] = Field(default_factory=list)
    on_success: list[dict[str, str]] = Field(default_factory=list)
    on_failure: list[dict[str, str]] = Field(default_factory=list)
    on_duration_warning_threshold_exceeded: list[dict[str, str]] = Field(default_factory=list)
    on_streaming_backlog_exceeded: list[dict[str, str]] = Field(default_factory=list)


class NotificationSettingsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    no_alert_for_skipped_runs: bool | None = None
    no_alert_for_canceled_runs: bool | None = None
    alert_on_last_attempt: bool | None = None


class HealthRuleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metric: str
    op: str
    value: int | float


class HealthConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rules: list[HealthRuleConfig] = Field(default_factory=list)


class JobTaskConfig(BaseModel):
    """One node in a multi-task job DAG."""

    model_config = ConfigDict(extra="forbid")

    task_key: str
    description: str | None = None
    depends_on: list[TaskDependency] = Field(default_factory=list)
    run_if: RunIf | None = None
    disabled: bool | None = None
    timeout_seconds: int | None = Field(default=None, ge=0)
    max_retries: int | None = Field(default=None, ge=0)
    min_retry_interval_millis: int | None = Field(default=None, ge=0)
    retry_on_timeout: bool | None = None
    environment_key: str | None = None
    # Compute: serverless environment_key XOR job_cluster_key XOR
    # existing_cluster_id XOR new_cluster (classic).
    existing_cluster_id: str | None = None
    job_cluster_key: str | None = None
    new_cluster: dict[str, Any] | None = None
    libraries: list[dict[str, Any]] = Field(default_factory=list)
    email_notifications: EmailNotificationsConfig | None = None
    webhook_notifications: WebhookNotificationsConfig | None = None
    notification_settings: NotificationSettingsConfig | None = None
    health: HealthConfig | None = None
    # Primary MVP task types
    notebook_task: NotebookTaskConfig | None = None
    spark_python_task: SparkPythonTaskConfig | None = None
    sql_task: SqlTaskConfig | None = None
    # Additional Jobs API task types (passthrough payload → SDK).
    python_wheel_task: dict[str, Any] | None = None
    spark_jar_task: dict[str, Any] | None = None
    spark_submit_task: dict[str, Any] | None = None
    pipeline_task: dict[str, Any] | None = None
    dbt_task: dict[str, Any] | None = None
    run_job_task: dict[str, Any] | None = None
    for_each_task: "ForEachTaskConfig | None" = None
    condition_task: dict[str, Any] | None = None
    dashboard_task: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _dbt_project_directory_is_not_local(self) -> "JobTaskConfig":
        """``dbt_task`` cannot source its project from the job directory.

        Every other ``./``-relative source the brick supports is a single
        file it can upload; a dbt project is a whole tree, and a partial
        upload would fail deep inside the dbt run instead of here.
        """
        project_directory = (self.dbt_task or {}).get("project_directory", "")
        if isinstance(project_directory, str) and project_directory.startswith("./"):
            raise ValueError(
                "dbt_task.project_directory cannot be a ./-relative path "
                f"({project_directory!r}): the Jobs brick uploads individual "
                "files, not directories. Point it at a workspace path such as "
                "/Workspace/Shared/<project>, or check the project out with "
                "git_source."
            )
        return self

    @model_validator(mode="after")
    def _exactly_one_task_type(self) -> "JobTaskConfig":
        configured = [
            self.notebook_task is not None,
            self.spark_python_task is not None,
            self.sql_task is not None,
            self.python_wheel_task is not None,
            self.spark_jar_task is not None,
            self.spark_submit_task is not None,
            self.pipeline_task is not None,
            self.dbt_task is not None,
            self.run_job_task is not None,
            self.for_each_task is not None,
            self.condition_task is not None,
            self.dashboard_task is not None,
        ]
        if sum(configured) != 1:
            raise ValueError(
                "exactly one task type is required "
                "(notebook_task, spark_python_task, sql_task, python_wheel_task, "
                "spark_jar_task, spark_submit_task, pipeline_task, dbt_task, "
                "run_job_task, for_each_task, condition_task, or dashboard_task)"
            )
        return self


class ForEachTaskConfig(BaseModel):
    """Fan-out body of a ``for_each_task``.

    Typed rather than a passthrough ``dict`` so the nested task is
    validated like a top-level one and its ``./``-relative sources are
    uploaded and path-rewritten on deploy (#406).
    """

    model_config = ConfigDict(extra="forbid")

    # A JSON-serialised array or a task-values reference such as
    # ``{{tasks.<key>.values.<name>}}`` -- the Jobs API takes a string,
    # not a YAML list.
    inputs: str
    concurrency: int | None = Field(default=None, gt=0)
    task: JobTaskConfig


JobTaskConfig.model_rebuild()


class FileArrivalConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    # Jobs API limits: min_time accepts 60, wait rejects 60 ("must be greater than 60 seconds").
    min_time_between_triggers_seconds: int | None = Field(default=None, ge=60)
    wait_after_last_change_seconds: int | None = Field(default=None, gt=60)

    @field_validator("url")
    @classmethod
    def _url_ends_with_slash(cls, url: str) -> str:
        if not url.endswith("/"):
            raise ValueError("file_arrival.url must end with '/' (Jobs API requirement)")
        return url


class PeriodicTriggerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    interval: int = Field(gt=0)
    unit: Literal["HOURS", "DAYS", "WEEKS"]


class TableUpdateTriggerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    table_names: list[str] = Field(min_length=1)
    # Must match the Jobs API ``Condition`` enum exactly: the SDK's
    # ``from_dict`` silently drops values it cannot map, so a typo here
    # reaches the API as a missing ``condition`` rather than an error.
    condition: Literal["ANY_UPDATED", "ALL_UPDATED"] | None = None
    min_time_between_triggers_seconds: int | None = Field(default=None, ge=0)
    wait_after_last_change_seconds: int | None = Field(default=None, ge=0)


class TriggerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_arrival: FileArrivalConfig | None = None
    periodic: PeriodicTriggerConfig | None = None
    table_update: TableUpdateTriggerConfig | None = None
    pause_status: PauseStatus = "UNPAUSED"

    @model_validator(mode="after")
    def _exactly_one_trigger_kind(self) -> "TriggerConfig":
        configured = [
            self.file_arrival is not None,
            self.periodic is not None,
            self.table_update is not None,
        ]
        if sum(configured) != 1:
            raise ValueError("exactly one of file_arrival, periodic, or table_update is required")
        return self


class ScheduleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quartz_cron_expression: str
    timezone_id: str = "UTC"
    pause_status: PauseStatus = "UNPAUSED"


class ContinuousConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pause_status: PauseStatus = "UNPAUSED"


class QueueConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True


class JobClusterConfig(BaseModel):
    """Shared classic cluster definition referenced by ``job_cluster_key``."""

    model_config = ConfigDict(extra="forbid")

    job_cluster_key: str
    new_cluster: dict[str, Any]


class JobEnvironmentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    environment_key: str
    spec: dict[str, Any] = Field(default_factory=dict)


class JobParameterConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    default: str = ""


class GitSourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    git_url: str
    git_provider: str
    git_branch: str | None = None
    git_tag: str | None = None
    git_commit: str | None = None


class RunAsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_name: str | None = None
    service_principal_name: str | None = None

    @model_validator(mode="after")
    def _exactly_one_identity(self) -> "RunAsConfig":
        if bool(self.user_name) == bool(self.service_principal_name):
            raise ValueError("exactly one of user_name or service_principal_name is required")
        return self


class JobConfig(BaseModel):
    """Authoring surface for ``jobs/<name>/job.yml`` (Jobs / Lakeflow Jobs API)."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str | None = None
    tasks: list[JobTaskConfig] = Field(min_length=1)
    job_clusters: list[JobClusterConfig] = Field(default_factory=list)
    environments: list[JobEnvironmentConfig] = Field(default_factory=list)
    parameters: list[JobParameterConfig] = Field(default_factory=list)
    tags: dict[str, str] = Field(default_factory=dict)
    schedule: ScheduleConfig | None = None
    trigger: TriggerConfig | None = None
    continuous: ContinuousConfig | None = None
    queue: QueueConfig | None = None
    max_concurrent_runs: int = Field(default=1, gt=0)
    timeout_seconds: int = Field(default=0, ge=0)
    email_notifications: EmailNotificationsConfig | None = None
    webhook_notifications: WebhookNotificationsConfig | None = None
    notification_settings: NotificationSettingsConfig | None = None
    health: HealthConfig | None = None
    git_source: GitSourceConfig | None = None
    run_as: RunAsConfig | None = None
    performance_target: Literal["PERFORMANCE_OPTIMIZED", "COST_OPTIMIZED"] | None = None
    budget_policy_id: str | None = None
    usage_policy_id: str | None = None

    @model_validator(mode="after")
    def _single_run_mode(self) -> "JobConfig":
        modes = [
            self.trigger is not None,
            self.schedule is not None,
            self.continuous is not None,
        ]
        if sum(modes) > 1:
            raise ValueError("trigger, schedule, and continuous are mutually exclusive")
        task_keys = [task.task_key for task in self.tasks]
        if len(task_keys) != len(set(task_keys)):
            raise ValueError("task_key values must be unique")
        return self


__all__ = [
    "ContinuousConfig",
    "EmailNotificationsConfig",
    "FileArrivalConfig",
    "ForEachTaskConfig",
    "GitSourceConfig",
    "HealthConfig",
    "HealthRuleConfig",
    "JobClusterConfig",
    "JobConfig",
    "JobEnvironmentConfig",
    "JobParameterConfig",
    "JobTaskConfig",
    "NotebookTaskConfig",
    "NotificationSettingsConfig",
    "PeriodicTriggerConfig",
    "QueueConfig",
    "RunAsConfig",
    "ScheduleConfig",
    "SparkPythonTaskConfig",
    "SqlAlertConfig",
    "SqlDashboardConfig",
    "SqlFileConfig",
    "SqlQueryConfig",
    "SqlTaskConfig",
    "TableUpdateTriggerConfig",
    "TaskDependency",
    "TriggerConfig",
    "WebhookNotificationsConfig",
]
