"""
Brick-registry consistency tests (issue #150).

The framework has two registries that must stay in sync:

* :func:`agent_foundry.bricks.iter_bricks` -- deploy ordering, used by
  the orchestrator.  Keys are historical brick names (e.g.
  ``data_domains``).
* :data:`agent_foundry.deploy._brick_registry.BRICK_ACTIONS` -- CLI
  action dispatch table for ``agent-foundry brick <name> <action>``.
  Keys are the directory / CLI name (e.g. ``data``).

Three bricks carry an historical name mismatch
(:data:`agent_foundry.deploy._brick_registry.DEPLOY_TO_CLI_NAME`).  This
test asserts:

1. The drift map is exhaustive -- every deploy-name that is not
   identity-mapped to a CLI name has an entry in the map.
2. Once translated through :func:`cli_name_for`, every brick that
   appears in the deploy registry also appears in ``BRICK_ACTIONS``.
3. The drift map has no stale entries (every deploy-name in it is
   actually emitted by ``iter_bricks``).

When a future PR aligns a brick's deploy-name with its CLI name, drop
the entry from ``DEPLOY_TO_CLI_NAME`` in the same PR; this test will
fail until the drift is removed *or* re-recorded.
"""

from __future__ import annotations

import pytest

from agent_foundry.bricks import iter_bricks
from agent_foundry.deploy._brick_registry import (
    BRICK_ACTIONS,
    DEPLOY_TO_CLI_NAME,
    UnknownBrickAction,
    cli_name_for,
    resolve_action,
)

# Bricks that legitimately exist in BRICK_ACTIONS but not in
# iter_bricks().  ``evaluation`` is a CLI verb (``agent-foundry brick
# evaluation run``) that does not participate in ``deploy``.  The
# previous ``app`` entry was deleted in #152 (the orphan ``bricks/app/``
# brick is gone).  ``skills`` and ``mcp`` keep their CLI verbs
# (``skills {assemble,validate}``, ``mcp {deploy,validate,inspect,resolve}``)
# but are no longer deploy bricks (issue #338): skill assembly and MCP
# server registration are capabilities of the agent payload's provision
# manifest.
CLI_ONLY_BRICKS: frozenset[str] = frozenset({"evaluation", "skills", "mcp"})


def test_deploy_bricks_have_cli_dispatch_entry() -> None:
    """Every brick the orchestrator runs must be reachable via
    ``agent-foundry brick <name> <action>``.  When the deploy-name
    differs from the CLI-name, :func:`cli_name_for` must bridge the gap.
    """
    cli_names = set(BRICK_ACTIONS)
    missing: list[tuple[str, str]] = []
    for brick in iter_bricks():
        cli = cli_name_for(brick.name)
        if cli not in cli_names:
            missing.append((brick.name, cli))
    assert not missing, (
        "These deploy-registry bricks have no entry in BRICK_ACTIONS "
        "(checked through cli_name_for):\n"
        + "\n".join(f"  iter_bricks={d!r} -> cli_name={c!r}" for d, c in missing)
        + "\nEither register CLI actions for the brick or extend "
        "DEPLOY_TO_CLI_NAME if it lives under a different directory."
    )


def test_drift_map_has_no_stale_entries() -> None:
    """Every key in :data:`DEPLOY_TO_CLI_NAME` must still be a real
    deploy-registry brick name.  Catches the inverse drift: a brick
    that was renamed in iter_bricks() but the migration entry was left
    behind.
    """
    deploy_names = {b.name for b in iter_bricks()}
    stale = sorted(set(DEPLOY_TO_CLI_NAME) - deploy_names)
    assert not stale, f"These DEPLOY_TO_CLI_NAME keys are no longer in iter_bricks() and should be removed: {stale}"


def test_drift_map_targets_exist_in_cli_registry() -> None:
    """Every value in :data:`DEPLOY_TO_CLI_NAME` must be a real CLI
    name (key of ``BRICK_ACTIONS``).  Catches a typo in the bridge
    that would silently route to a non-existent CLI brick.
    """
    cli_names = set(BRICK_ACTIONS)
    bad = sorted(f"{deploy_name} -> {cli}" for deploy_name, cli in DEPLOY_TO_CLI_NAME.items() if cli not in cli_names)
    assert not bad, f"These DEPLOY_TO_CLI_NAME mappings target a CLI name not in BRICK_ACTIONS: {bad}"


def test_cli_only_bricks_are_documented() -> None:
    """Every CLI-registry brick that has no deploy entry must be in
    :data:`CLI_ONLY_BRICKS`.  Forces a docs touch when the next CLI-only
    brick is added so the maintainer has to think about whether the
    new brick should also participate in deploy.
    """
    deploy_cli_names = {cli_name_for(b.name) for b in iter_bricks()}
    cli_only = set(BRICK_ACTIONS) - deploy_cli_names
    undocumented = sorted(cli_only - CLI_ONLY_BRICKS)
    assert not undocumented, (
        "These bricks are in BRICK_ACTIONS but not iter_bricks() and "
        "are not documented as CLI-only in CLI_ONLY_BRICKS: "
        f"{undocumented}.  If they are intentionally CLI-only, add them "
        "to CLI_ONLY_BRICKS in this test.  Otherwise register them with "
        "iter_bricks() (or extend DEPLOY_TO_CLI_NAME)."
    )


def test_cli_name_for_is_identity_when_aligned() -> None:
    """:func:`cli_name_for` is identity for any name not in the drift
    map -- guards against an accidental general transformation
    creeping into the helper.
    """
    assert cli_name_for("warehouse") == "warehouse"
    assert cli_name_for("app") == "app"
    assert cli_name_for("not_a_brick_anywhere") == "not_a_brick_anywhere"
    assert cli_name_for("data_domains") == "data"


# ---------------------------------------------------------------------------
# Renamed CLI verbs (issue #338)
# ---------------------------------------------------------------------------


def test_tracing_replaced_agent_in_both_registries() -> None:
    """The misnamed ``agent`` brick is gone; ``tracing`` is the aligned
    name in both registries (identity through ``cli_name_for``)."""
    assert "tracing" in BRICK_ACTIONS
    assert "agent" not in BRICK_ACTIONS
    assert "tracing" in {b.name for b in iter_bricks()}
    assert "agent" not in {b.name for b in iter_bricks()}


def test_old_agent_verb_does_not_resolve() -> None:
    """The pre-#338 ``agent`` verb (now ``tracing``) has no alias."""
    with pytest.raises(UnknownBrickAction):
        resolve_action("agent", "ensure-experiment")


def test_app_replaced_agent_app_in_both_registries() -> None:
    """The Databricks App brick is now keyed ``app`` in both registries
    (issue #338); the pre-#338 ``agent_app`` key is gone."""
    assert "app" in BRICK_ACTIONS
    assert "agent_app" not in BRICK_ACTIONS
    assert "app" in {b.name for b in iter_bricks()}
    assert "agent_app" not in {b.name for b in iter_bricks()}


def test_old_agent_app_verb_does_not_resolve() -> None:
    """The pre-#338 ``agent_app`` verb (now ``app``) has no alias."""
    with pytest.raises(UnknownBrickAction):
        resolve_action("agent_app", "deploy")
