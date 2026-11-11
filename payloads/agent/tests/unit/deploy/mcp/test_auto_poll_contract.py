"""Contract tests for :class:`AutoPollConfig` and per-type defaults.

Auto-poll is a wrapper-side feature that collapses async ``submit ->
poll`` MCP tool patterns into one transparent call.  This file pins
the user-facing shape (YAML / Python kwargs) and the per-type defaults
that ship out of the box for ``managed_sql`` and ``managed_genie``.

Wrapper behaviour is tested in ``bricks/agent/tests/test_mcp_wrapper.py``;
state round-trip is tested in ``payloads/agent/tests/unit/deploy/mcp/test_deploy.py``.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent_foundry.contracts.mcp import (
    ArgPassthrough,
    AutoPollConfig,
    ExternalServerConfig,
    ManagedFunctionsServerConfig,
    ManagedGenieServerConfig,
    ManagedSqlServerConfig,
    ToolsConfig,
)

# ---------------------------------------------------------------------------
# AutoPollConfig direct parsing
# ---------------------------------------------------------------------------


class TestAutoPollConfigParsing:
    def test_minimal_valid_config(self) -> None:
        cfg = AutoPollConfig(
            trigger="submit",
            poll_with="poll",
            forward_args=[ArgPassthrough(from_field="id", to_arg="id")],
        )
        assert cfg.trigger == "submit"
        assert cfg.poll_with == "poll"
        assert cfg.forward_args[0].from_field == "id"
        # Defaults preserved across the spec:
        assert cfg.state_field == "status.state"
        assert cfg.pending_states == ["PENDING"]
        assert cfg.timeout_s == 60.0
        assert cfg.hide_poll_tool is True

    def test_trigger_must_differ_from_poll_with(self) -> None:
        with pytest.raises(ValidationError, match="must differ"):
            AutoPollConfig(
                trigger="x",
                poll_with="x",
                forward_args=[ArgPassthrough(from_field="a", to_arg="b")],
            )

    def test_forward_args_must_be_non_empty(self) -> None:
        with pytest.raises(ValidationError, match="forward_args"):
            AutoPollConfig(trigger="s", poll_with="p", forward_args=[])

    def test_trigger_and_poll_with_must_be_non_empty(self) -> None:
        with pytest.raises(ValidationError):
            AutoPollConfig(
                trigger="",
                poll_with="p",
                forward_args=[ArgPassthrough(from_field="a", to_arg="b")],
            )

    def test_arg_passthrough_fields_must_be_non_empty(self) -> None:
        with pytest.raises(ValidationError):
            ArgPassthrough(from_field="", to_arg="x")
        with pytest.raises(ValidationError):
            ArgPassthrough(from_field="x", to_arg="")

    def test_positive_timeout_required(self) -> None:
        with pytest.raises(ValidationError):
            AutoPollConfig(
                trigger="s",
                poll_with="p",
                forward_args=[ArgPassthrough(from_field="a", to_arg="b")],
                timeout_s=0,
            )


# ---------------------------------------------------------------------------
# Per-type defaults: bool tri-state collapses to AutoPollConfig | None
# ---------------------------------------------------------------------------


class TestPerTypeDefaults:
    """``auto_poll: true|false|<dict>`` resolves consistently per type."""

    def test_managed_sql_default_is_enabled_with_sql_shape(self) -> None:
        # ``auto_poll`` defaults to True at the field level; the
        # validator collapses it to the type's ``_DEFAULT_AUTO_POLL``.
        # Read through ``resolved_auto_poll`` so the post-validator
        # type (``AutoPollConfig | None``) is what assertions see.
        cfg = ManagedSqlServerConfig(
            tools=ToolsConfig(allow=["execute_sql_read_only", "poll_sql_result"]),
        )
        ap = cfg.resolved_auto_poll
        assert ap is not None
        assert ap.trigger == "execute_sql_read_only"
        assert ap.poll_with == "poll_sql_result"
        # SQL-specific shape: single statement_id forward.
        assert [(f.from_field, f.to_arg) for f in ap.forward_args] == [
            ("statement_id", "statement_id"),
        ]
        assert "PENDING" in ap.pending_states

    def test_managed_genie_default_is_enabled_with_genie_shape(self) -> None:
        cfg = ManagedGenieServerConfig(
            space_id="sp_1",
            tools=ToolsConfig(allow=["query_space", "poll_response"]),
        )
        ap = cfg.resolved_auto_poll
        assert ap is not None
        assert ap.trigger == "query_space"
        assert ap.poll_with == "poll_response"
        # Genie's poll requires BOTH conversation_id and message_id.
        # Response uses camelCase (conversationId/messageId), poll
        # tool params are snake_case -- this asymmetry is the actual
        # upstream contract (verified against live MCP server).
        assert [(f.from_field, f.to_arg) for f in ap.forward_args] == [
            ("conversationId", "conversation_id"),
            ("messageId", "message_id"),
        ]
        # Genie pending state lives at top-level ``status`` (per the
        # upstream API), not under ``status.state`` like SQL.
        assert ap.state_field == "status"
        # PENDING_WAREHOUSE is critical -- Genie's *first* response
        # is always this status; skipping it breaks the entire cycle.
        assert "PENDING_WAREHOUSE" in ap.pending_states
        assert "ASKING_AI" in ap.pending_states
        # Matches ``bricks/agent/tools.py:_GENIE_TIMEOUT`` (5 minutes).
        assert ap.timeout_s == 300.0

    def test_explicit_false_disables_per_type_default(self) -> None:
        cfg = ManagedSqlServerConfig(
            tools=ToolsConfig(allow=["x"]),
            auto_poll=False,
        )
        assert cfg.resolved_auto_poll is None

    def test_explicit_dict_overrides_per_type_default(self) -> None:
        # Custom auto_poll completely replaces the SQL default; the
        # type-level _DEFAULT_AUTO_POLL is NOT merged in.
        cfg = ManagedSqlServerConfig(
            tools=ToolsConfig(allow=["x"]),
            auto_poll={
                "trigger": "custom_submit",
                "poll_with": "custom_poll",
                "forward_args": [{"from_field": "tx_id", "to_arg": "tx"}],
                "timeout_s": 5.0,
            },
        )
        ap = cfg.resolved_auto_poll
        assert ap is not None
        assert ap.trigger == "custom_submit"
        assert ap.poll_with == "custom_poll"
        assert ap.timeout_s == 5.0

    def test_external_type_with_true_resolves_to_none(self) -> None:
        # ``external`` and ``custom_app`` ship no auto-poll default,
        # so ``auto_poll: true`` (or omitted) still resolves to None.
        # Users must supply an explicit dict to opt in.
        cfg = ExternalServerConfig(
            connection_name="x",
            tools=ToolsConfig(allow=["y"]),
            auto_poll=True,
        )
        assert cfg.resolved_auto_poll is None

    def test_external_type_can_opt_in_via_explicit_dict(self) -> None:
        cfg = ExternalServerConfig(
            connection_name="x",
            tools=ToolsConfig(allow=["y"]),
            auto_poll={
                "trigger": "submit",
                "poll_with": "fetch",
                "forward_args": [{"from_field": "id", "to_arg": "id"}],
            },
        )
        ap = cfg.resolved_auto_poll
        assert ap is not None
        assert ap.trigger == "submit"

    def test_managed_functions_has_no_default(self) -> None:
        # UC functions are synchronous; auto-poll makes no sense.
        cfg = ManagedFunctionsServerConfig(
            catalog="c",
            schema="s",
            tools=ToolsConfig(allow=["fn"]),
        )
        assert cfg.resolved_auto_poll is None
