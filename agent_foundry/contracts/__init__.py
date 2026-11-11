"""Typed data contracts for agent-foundry YAML files (issue #67).

This subpackage exposes Pydantic models that mirror the on-disk YAML shapes
consumed by the framework. It is strictly additive: nothing in this
subpackage imports from or mutates existing runtime state, so landing it has
no behavioural impact. Follow-up PRs migrate individual bricks to consume
these models (see tracking issues #67-A through #67-E).

All names re-exported here are part of the public ``agent_foundry`` API and
follow SemVer once the package is released as a library. Everything that is
not re-exported is internal.
"""

from agent_foundry.contracts.a2a import (
    A2AAgentCapabilities,
    A2AAgentCardConfig,
    A2AAgentProvider,
    A2AAgentSkill,
    A2AConfig,
)
from agent_foundry.contracts.agent_app import (
    AgentAppConfig,
)
from agent_foundry.contracts.jobs import (
    ContinuousConfig,
    EmailNotificationsConfig,
    FileArrivalConfig,
    ForEachTaskConfig,
    GitSourceConfig,
    HealthConfig,
    HealthRuleConfig,
    JobClusterConfig,
    JobConfig,
    JobEnvironmentConfig,
    JobParameterConfig,
    JobTaskConfig,
    NotebookTaskConfig,
    NotificationSettingsConfig,
    PeriodicTriggerConfig,
    QueueConfig,
    RunAsConfig,
    ScheduleConfig,
    SparkPythonTaskConfig,
    SqlAlertConfig,
    SqlDashboardConfig,
    SqlFileConfig,
    SqlQueryConfig,
    SqlTaskConfig,
    TableUpdateTriggerConfig,
    TaskDependency,
    TriggerConfig,
    WebhookNotificationsConfig,
)
from agent_foundry.contracts.mcp import (
    CustomAppServerConfig,
    ExternalServerConfig,
    ManagedFunctionsServerConfig,
    ManagedGenieServerConfig,
    ManagedSqlServerConfig,
    ManagedVectorSearchServerConfig,
    PromptsConfig,
    ResourcesConfig,
    ServerConfig,
    ToolsConfig,
)
from agent_foundry.contracts.payload import (
    PayloadBrickRequirement,
    PayloadConfig,
    PayloadFile,
)
from agent_foundry.contracts.skills import (
    SkillDomainMetadata,
    SkillMetadata,
)
from agent_foundry.contracts.tools import (
    GenieTool,
    MCPTool,
    PythonFunctionTool,
    Tool,
    ToolManifest,
    UCFunctionTool,
    VectorSearchTool,
)
from agent_foundry.contracts.vector_search import (
    DeltaSyncIndex,
    DeltaSyncSpec,
    DirectAccessIndex,
    DirectAccessSpec,
    EmbeddingSourceColumn,
    EmbeddingVectorColumn,
    EndpointConfig,
    IndexConfig,
    IndexSpec,
)

__all__ = [
    "A2AAgentCapabilities",
    "A2AAgentCardConfig",
    "A2AAgentProvider",
    "A2AAgentSkill",
    "A2AConfig",
    "AgentAppConfig",
    "ContinuousConfig",
    "CustomAppServerConfig",
    "DeltaSyncIndex",
    "DeltaSyncSpec",
    "DirectAccessIndex",
    "DirectAccessSpec",
    "EmailNotificationsConfig",
    "EmbeddingSourceColumn",
    "EmbeddingVectorColumn",
    "EndpointConfig",
    "ExternalServerConfig",
    "FileArrivalConfig",
    "ForEachTaskConfig",
    "GenieTool",
    "GitSourceConfig",
    "HealthConfig",
    "HealthRuleConfig",
    "IndexConfig",
    "IndexSpec",
    "JobClusterConfig",
    "JobConfig",
    "JobEnvironmentConfig",
    "JobParameterConfig",
    "JobTaskConfig",
    "MCPTool",
    "ManagedFunctionsServerConfig",
    "ManagedGenieServerConfig",
    "ManagedSqlServerConfig",
    "ManagedVectorSearchServerConfig",
    "NotebookTaskConfig",
    "NotificationSettingsConfig",
    "PayloadBrickRequirement",
    "PayloadConfig",
    "PayloadFile",
    "PeriodicTriggerConfig",
    "PromptsConfig",
    "PythonFunctionTool",
    "QueueConfig",
    "ResourcesConfig",
    "RunAsConfig",
    "ServerConfig",
    "ScheduleConfig",
    "SkillDomainMetadata",
    "SkillMetadata",
    "SparkPythonTaskConfig",
    "SqlAlertConfig",
    "SqlDashboardConfig",
    "SqlFileConfig",
    "SqlQueryConfig",
    "SqlTaskConfig",
    "TableUpdateTriggerConfig",
    "TaskDependency",
    "Tool",
    "ToolManifest",
    "ToolsConfig",
    "TriggerConfig",
    "UCFunctionTool",
    "VectorSearchTool",
    "WebhookNotificationsConfig",
]
