"""Public API for the agent-foundry framework.

Anything re-exported here is the documented public surface. These names
follow SemVer once the package is published as a library (issue #66).
Everything else is internal and may change without notice.

Consumers should prefer::

    from agent_foundry import InstanceConfig, AgentConfig

over reaching into submodules (``agent_foundry.config.InstanceConfig``),
unless they need a symbol that is intentionally kept private.
"""

from agent_foundry.bricks import (
    Brick,
    BrickResult,
    DeployContext,
    DestroyContext,
    DestroyPlan,
    DestroyPlanItem,
    ResourceRef,
    ScaffoldResult,
    iter_bricks,
)
from agent_foundry.config import (
    TAG_KEY_MANAGED_BY,
    DeploymentState,
    EvaluationConfig,
    InstanceConfig,
    TargetConfig,
    WarehouseConfig,
    load_state_outputs,
)
from agent_foundry.contracts import (
    GenieTool,
    SkillDomainMetadata,
    SkillMetadata,
    Tool,
    ToolManifest,
    UCFunctionTool,
    VectorSearchTool,
)
from agent_foundry.deploy import (
    deploy_instance,
    destroy_instance,
    discover_instances,
)
from agent_foundry.loader import (
    ConfigNotFoundError,
    FoundryConfigError,
    PartialMigrationError,
    load_instance_config,
    load_repo_config,
)
from agent_foundry.public import (
    CURRENT_SCHEMA_VERSION,
    DeployResult,
    DestroyResult,
    FoundryInstanceConfig,
    FoundryRepoConfig,
    InstanceRef,
    StateOutputs,
    Target,
    Warehouse,
)
from agent_foundry.state import BrickStateValidationError

__all__ = [
    "Brick",
    "BrickResult",
    "BrickStateValidationError",
    "CURRENT_SCHEMA_VERSION",
    "ConfigNotFoundError",
    "DeployContext",
    "DestroyContext",
    "DestroyPlan",
    "DestroyPlanItem",
    "DestroyResult",
    "DeployResult",
    "DeploymentState",
    "EvaluationConfig",
    "FoundryConfigError",
    "FoundryInstanceConfig",
    "FoundryRepoConfig",
    "GenieTool",
    "InstanceConfig",
    "InstanceRef",
    "PartialMigrationError",
    "ResourceRef",
    "ScaffoldResult",
    "SkillDomainMetadata",
    "SkillMetadata",
    "StateOutputs",
    "TAG_KEY_MANAGED_BY",
    "Target",
    "TargetConfig",
    "Tool",
    "ToolManifest",
    "UCFunctionTool",
    "VectorSearchTool",
    "Warehouse",
    "WarehouseConfig",
    "deploy_instance",
    "destroy_instance",
    "discover_instances",
    "iter_bricks",
    "load_instance_config",
    "load_repo_config",
    "load_state_outputs",
]
