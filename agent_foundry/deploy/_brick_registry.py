"""
Brick action dispatch table (issue #96).

Single source of truth that maps ``<brick> <action>`` (as the user types
on the CLI) to the dotted ``module:callable`` target on disk.  Used by
``agent-foundry brick <name> <action>`` to forward arguments to the
underlying brick script's ``main(argv)``.

Why a flat table and not ``Brick.actions`` on the registry protocol?

- Keeping the table here lets ``agent-foundry brick --help`` and
  ``agent-foundry destroy`` enumerate available actions without having
  to instantiate every ``Brick`` (which is slow and pulls in deploy-time
  imports).
- Migrating to ``Brick.actions: dict[str, Callable]`` is tracked as a
  follow-up issue; doing it here keeps this PR focused on CLI
  consolidation.

Naming-drift bridge (issue #150)
================================

Three bricks carry historical names in :func:`agent_foundry.bricks.iter_bricks`
that differ from the directory under ``bricks/`` and the CLI verb in
:data:`BRICK_ACTIONS` above.  :data:`DEPLOY_TO_CLI_NAME` documents the
mapping.  A future rename (or wholesale registry merge) is tracked by
issue #150's follow-up.  The consistency test in
``tests/contracts/test_registry_consistency.py`` pins the mapping so new drift
fails loudly.
"""

from __future__ import annotations

import importlib
from typing import Callable, Final

BRICK_ACTIONS: dict[str, dict[str, str]] = {
    "warehouse": {
        "deploy": "bricks.warehouse.scripts.deploy_warehouse:main",
    },
    "data": {
        "deploy": "bricks.data.scripts.deploy_views:main",
    },
    "sql_function": {
        "deploy": "bricks.sql_function.scripts.deploy_functions:main",
    },
    "genie": {
        "assemble": "bricks.genie.scripts.assemble:main",
        "deploy": "bricks.genie.scripts.deploy:main",
        "export": "bricks.genie.scripts.export:main",
        "validate": "bricks.genie.scripts.validate:main",
    },
    "vector_search": {
        "deploy-index": "bricks.vector_search.scripts.deploy_index:main",
        "export-index": "bricks.vector_search.scripts.export_index:main",
        "validate-index": "bricks.vector_search.scripts.validate_index:main",
    },
    "model_serving": {
        "deploy": "bricks.model_serving.scripts.deploy_endpoint:main",
    },
    "job": {
        "deploy": "bricks.job.scripts.deploy_job:main",
        "validate": "bricks.job.scripts.validate_job:main",
    },
    # ``skills`` and ``mcp`` keep their standalone CLI verbs but are no
    # longer deploy bricks (issue #338): skill assembly and MCP server
    # registration are capabilities of the agent payload, run by
    # ``AgentPayloadAdapter.provision`` via the payload's PROVISION_STEPS
    # manifest.  These verbs remain so authors can assemble / validate /
    # inspect out of band; the orchestrated ``app deploy`` path runs the
    # same code through the payload.
    "skills": {
        "assemble": "payloads.agent.deploy.skills.assemble:main",
        "validate": "payloads.agent.deploy.skills.validate:main",
    },
    # ``lakebase`` was removed as a standalone brick action: chat
    # history is provisioned by ``app deploy`` when
    # ``[chat_history] enabled = true``.
    "mcp": {
        "deploy": "payloads.agent.deploy.mcp.deploy:main",
        "validate": "payloads.agent.deploy.mcp.validate:main",
        "inspect": "payloads.agent.deploy.mcp.inspect:main",
        "resolve": "payloads.agent.deploy.mcp.resolve:main",
    },
    "ai_gateway": {
        "deploy": "bricks.ai_gateway.scripts.skills:main",
        "import": "bricks.ai_gateway.scripts.import_skills:main",
    },
    "tracing": {
        "ensure-experiment": "bricks.tracing.scripts.ensure_experiment:main",
    },
    "app": {
        "deploy": "bricks.app.scripts.deploy_agent_app:main",
        "start-app": "bricks.app.scripts.start_app:main",
        "start-server": "payloads.agent.runtime.start_server:main",
    },
    "observability": {
        "deploy-dashboard": "bricks.observability.scripts.deploy_dashboard:main",
    },
    "evaluation": {
        "run": "bricks.evaluation.scripts.run_evaluation:main",
        "monitors": "bricks.evaluation.scripts.register_monitors:main",
    },
}


# ---------------------------------------------------------------------------
# Naming-drift bridge between iter_bricks() and BRICK_ACTIONS  (issue #150)
# ---------------------------------------------------------------------------
#
# ``iter_bricks()`` keys (deploy-time):  ``data_domains``, ``genie_spaces``,
# ``vector_indexes``.  Directory + CLI keys: ``data``, ``genie``,
# ``vector_search``.  When a future PR renames the deploy-time keys to
# match the directory (likely batched with #151/#152), drop the entry
# from this map at the same time.

DEPLOY_TO_CLI_NAME: Final[dict[str, str]] = {
    "data_domains": "data",
    "genie_spaces": "genie",
    "jobs": "job",
    "vector_indexes": "vector_search",
}


def cli_name_for(deploy_name: str) -> str:
    """Return the CLI / directory name for a deploy-registry brick name.

    Identity for bricks that are already aligned; uses
    :data:`DEPLOY_TO_CLI_NAME` for the documented historical-drift cases.
    """
    return DEPLOY_TO_CLI_NAME.get(deploy_name, deploy_name)


def deploy_name_for(name: str) -> str | None:
    """Resolve a CLI or deploy-registry brick name to the registry key.

    Returns ``None`` when *name* does not match any registered brick.
    """
    from agent_foundry.bricks import iter_bricks

    registry_names = {b.name for b in iter_bricks()}
    if name in registry_names:
        return name
    for deploy_name, cli_name in DEPLOY_TO_CLI_NAME.items():
        if name == cli_name:
            return deploy_name
    return None


def list_bricks() -> list[str]:
    return sorted(BRICK_ACTIONS)


class UnknownBrickAction(ValueError):
    """Raised by :func:`resolve_action` when the brick or action is unknown."""


def resolve_action(brick: str, action: str) -> Callable[[list[str] | None], int]:
    """Resolve ``<brick> <action>`` to the brick script's ``main(argv)``.

    Raises
    ------
    UnknownBrickAction
        With a friendly message listing valid choices when *brick* or
        *action* is unknown.
    """
    actions = BRICK_ACTIONS.get(brick)
    if actions is None:
        raise UnknownBrickAction(f"Unknown brick {brick!r}. Choose from: {', '.join(list_bricks())}")
    target = actions.get(action)
    if target is None:
        raise UnknownBrickAction(
            f"Unknown action {action!r} for brick {brick!r}. Choose from: {', '.join(sorted(actions))}"
        )
    module_name, _, attr = target.partition(":")
    module = importlib.import_module(module_name)
    main: Callable[[list[str] | None], int] = getattr(module, attr)
    return main
