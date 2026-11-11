"""Tests for ``payloads/agent/deploy/mcp/deploy.py``.

Covers the workspace-only persistence contract: the deploy script must
call :func:`agent_foundry.state.save_state` on every successful deploy
and must NOT create any local ``_state/`` directory (issue #50: the
workspace is the single source of truth, stale per-fork local files
must not override live state at deploy time and must not be bundled
into deployed app source via ``databricks sync``).

The unit tests here pin three behaviours that the runtime depends on:

1. A successful ``deploy()`` (non-dry-run) calls ``save_state`` with
   the right positional / keyword args -- including the ``state``
   payload reflecting the resolved URL, name_prefix, and the full
   tools/prompts/resources blocks.
2. ``deploy()`` never creates a ``<sub_dir>/_state/`` directory.
3. ``dry_run=True`` short-circuits before any workspace client is
   constructed and before ``save_state`` is called, so unauthenticated
   ``--dry-run`` from CI / local inspection works.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from agent_foundry._instance_layout import resolve_payload_subdir
from payloads.agent.deploy.mcp import deploy as deploy_mod
from payloads.agent.tests.unit.deploy.mcp.conftest import write_server_yml

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MANAGED_SQL_YML = """
type: managed_sql
description: warehouse for tests
tools:
  allow:
    - execute_sql_read_only
    - poll_sql_result
"""

_MANAGED_GENIE_YML = """
type: managed_genie
space_id: sp_test_123
tools:
  allow:
    - match: "query_space_*"
      expose_as: query_space
    - match: "poll_response_*"
      expose_as: poll_response
"""


def _build_sub(tmp_path: Path, name: str, body: str) -> Path:
    """Construct a realistic ``instances/<app>/payloads/agent/mcp_servers/<sub>/`` shell.

    The ``instance_name`` derived inside :func:`deploy` is
    ``sub.parents[3].name`` (the instance dir that owns the payload tree),
    so the server must be nested under the agent payload root.
    """
    instance_dir = tmp_path / "instances" / "myapp"
    return write_server_yml(resolve_payload_subdir(instance_dir, "mcp_servers"), name, body)


def _vars(host: str = "https://ws.example.com") -> dict[str, str]:
    return {"host": host, "deployer": "tester@example.com"}


# ---------------------------------------------------------------------------
# Workspace persistence (the bug fix this PR pins down)
# ---------------------------------------------------------------------------


class TestWorkspacePersistence:
    """Every successful ``deploy()`` must reach the workspace."""

    def test_calls_save_state_with_resolved_state_payload(
        self,
        tmp_path: Path,
    ) -> None:
        sub = _build_sub(tmp_path, "warehouse", _MANAGED_SQL_YML)

        with (
            patch.object(
                deploy_mod,
                "_get_client",
                return_value=MagicMock(),
            ),
            patch.object(deploy_mod, "save_state") as mock_save,
        ):
            rc = deploy_mod.deploy(sub, "dev", _vars())

        assert rc == 0
        mock_save.assert_called_once()

        # Positional args: (ws, instance_name, brick_type, sub, target, state).
        args, kwargs = mock_save.call_args
        assert args[1] == "myapp"
        assert args[2] == "mcp_servers"
        assert args[3] == "warehouse"
        assert args[4] == "dev"

        state = args[5]
        assert state["type"] == "managed_sql"
        assert state["url"] == "https://ws.example.com/api/2.0/mcp/sql"
        assert state["name_prefix"] == "warehouse__"
        # Bare-string allow entries must round-trip verbatim so the
        # runtime wrapper reproduces the LLM-visible identity exactly.
        assert state["tools"]["allow"] == [
            "execute_sql_read_only",
            "poll_sql_result",
        ]
        # Prompts/resources blocks are present (with disabled defaults)
        # so the runtime never has to handle a missing key.
        assert state["prompts"]["enabled"] is False
        assert state["resources"]["enabled"] is False

        # Audit kwargs are preserved so the workspace state file
        # carries a deploy-event audit trail.
        assert kwargs["action"] == "deploy"
        assert kwargs["deployer"] == "tester@example.com"
        assert kwargs["resource_id"] == state["url"]

    def test_auto_poll_persists_in_state_when_enabled(
        self,
        tmp_path: Path,
    ) -> None:
        # ``managed_sql`` opts into auto-poll by default, so the state
        # JSON must carry the resolved AutoPollConfig (otherwise the
        # runtime can't dispatch the internal poll loop).
        sub = _build_sub(tmp_path, "warehouse", _MANAGED_SQL_YML)

        with (
            patch.object(
                deploy_mod,
                "_get_client",
                return_value=MagicMock(),
            ),
            patch.object(deploy_mod, "save_state") as mock_save,
        ):
            rc = deploy_mod.deploy(sub, "dev", _vars())

        assert rc == 0
        state = mock_save.call_args.args[5]
        assert "auto_poll" in state, "auto_poll must round-trip into state"
        ap = state["auto_poll"]
        assert ap["trigger"] == "execute_sql_read_only"
        assert ap["poll_with"] == "poll_sql_result"
        # Forward-args list must serialise back as list of dicts so
        # the runtime can re-validate it via Pydantic.
        assert ap["forward_args"] == [
            {"from_field": "statement_id", "to_arg": "statement_id"},
        ]

    def test_auto_poll_absent_when_disabled(self, tmp_path: Path) -> None:
        # When auto_poll is explicitly disabled, the state must not
        # carry the key at all -- absence is the runtime's signal to
        # skip auto-poll handling entirely.
        body = _MANAGED_SQL_YML + "auto_poll: false\n"
        sub = _build_sub(tmp_path, "warehouse", body)

        with (
            patch.object(
                deploy_mod,
                "_get_client",
                return_value=MagicMock(),
            ),
            patch.object(deploy_mod, "save_state") as mock_save,
        ):
            rc = deploy_mod.deploy(sub, "dev", _vars())

        assert rc == 0
        state = mock_save.call_args.args[5]
        assert "auto_poll" not in state

    def test_dict_form_allow_entries_round_trip(self, tmp_path: Path) -> None:
        # The wrapper runtime path expects ``{match, expose_as}``
        # entries to survive the deploy untouched -- otherwise glob
        # matching breaks and the LLM-visible aliases collapse.
        sub = _build_sub(tmp_path, "genie", _MANAGED_GENIE_YML)

        with (
            patch.object(
                deploy_mod,
                "_get_client",
                return_value=MagicMock(),
            ),
            patch.object(deploy_mod, "save_state") as mock_save,
        ):
            rc = deploy_mod.deploy(sub, "dev", _vars())

        assert rc == 0
        state = mock_save.call_args.args[5]
        assert state["tools"]["allow"] == [
            {"match": "query_space_*", "expose_as": "query_space"},
            {"match": "poll_response_*", "expose_as": "poll_response"},
        ]

    def test_does_not_create_local_state_directory(
        self,
        tmp_path: Path,
    ) -> None:
        # Workspace is the single source of truth (issue #50).  A leaked
        # local _state/ directory would be picked up by ``databricks
        # sync`` when the app source is uploaded and then shadow live
        # workspace state on the next deploy.
        sub = _build_sub(tmp_path, "warehouse", _MANAGED_SQL_YML)

        with (
            patch.object(
                deploy_mod,
                "_get_client",
                return_value=MagicMock(),
            ),
            patch.object(deploy_mod, "save_state"),
        ):
            deploy_mod.deploy(sub, "dev", _vars())

        assert not (sub / "_state").exists(), (
            "deploy() leaked a local _state directory; framework convention is workspace-only (see issue #50)"
        )


# ---------------------------------------------------------------------------
# Dry-run short-circuit
# ---------------------------------------------------------------------------


class TestDryRun:
    """``dry_run=True`` must skip every workspace touchpoint."""

    def test_does_not_construct_workspace_client(
        self,
        tmp_path: Path,
    ) -> None:
        sub = _build_sub(tmp_path, "warehouse", _MANAGED_SQL_YML)

        with (
            patch.object(deploy_mod, "_get_client") as mock_client,
            patch.object(deploy_mod, "save_state") as mock_save,
        ):
            rc = deploy_mod.deploy(sub, "dev", _vars(), dry_run=True)

        assert rc == 0
        mock_client.assert_not_called()
        mock_save.assert_not_called()
        assert not (sub / "_state").exists()


# ---------------------------------------------------------------------------
# Workspace-path log line (downstream tooling greps it for the path)
# ---------------------------------------------------------------------------


class TestWorkspacePathHelper:
    """The ``_workspace_state_path`` helper must mirror the canonical layout
    used by ``agent_foundry.state._ws_state_path`` -- if the layouts ever
    diverge, the print-line in the deploy log points at a bogus path.
    """

    def test_canonical_layout(self) -> None:
        path = deploy_mod._workspace_state_path("myapp", "warehouse", "dev")
        assert path == ("/Shared/agent-foundry/myapp/_state/mcp_servers/warehouse/dev.json")

    def test_matches_state_module_layout(self) -> None:
        # Defensive: import the private helper and compare directly so a
        # silent rename in agent_foundry.state surfaces here rather than
        # in a confusing prod log line.
        from agent_foundry.state import _ws_state_path

        assert deploy_mod._workspace_state_path(
            "myapp",
            "warehouse",
            "dev",
        ) == _ws_state_path("myapp", "mcp_servers", "warehouse", "dev")
