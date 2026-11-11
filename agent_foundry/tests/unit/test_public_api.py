"""Tripwire tests for the public ``agent_foundry`` API (issue #66).

These tests guard the contract between the framework and its downstream
consumers. If you need to remove or rename a symbol in ``__all__`` that
is a breaking change and requires a CHANGELOG entry and a deprecation
cycle.
"""

from __future__ import annotations

import agent_foundry


def test_all_names_are_actually_exported():
    """Every name in ``__all__`` must be importable from the top-level package."""
    for name in agent_foundry.__all__:
        assert hasattr(agent_foundry, name), (
            f"agent_foundry.__all__ lists {name!r} but the package has no such attribute"
        )


def test_public_api_surface_is_stable():
    """The set of names in ``__all__`` is a tripwire for intentional breakage.

    If this test fails, either (a) you are adding a new public symbol --
    update the expected set and document it -- or (b) you are removing a
    public symbol, which is a SemVer-breaking change and needs a
    deprecation cycle and CHANGELOG entry.
    """
    expected = {
        "Brick",
        "BrickResult",
        "BrickStateValidationError",
        "CURRENT_SCHEMA_VERSION",
        "ConfigNotFoundError",
        "DeployContext",
        "DeployResult",
        "DestroyContext",
        "DestroyPlan",
        "DestroyPlanItem",
        "DestroyResult",
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
    }
    assert set(agent_foundry.__all__) == expected


def test_top_level_imports_round_trip_with_submodule():
    """``from agent_foundry import X`` must be the same object as
    ``from agent_foundry.<submodule> import X``."""
    from agent_foundry.config import (
        InstanceConfig,
        TargetConfig,
    )
    from agent_foundry.contracts import (
        GenieTool,
        ToolManifest,
    )

    assert agent_foundry.InstanceConfig is InstanceConfig
    assert agent_foundry.TargetConfig is TargetConfig
    assert agent_foundry.GenieTool is GenieTool
    assert agent_foundry.ToolManifest is ToolManifest
