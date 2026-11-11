"""Tests for :mod:`payloads.agent.runtime.responses_input`."""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest

from payloads.agent.runtime.responses_input import (
    restore_tool_output_lists,
    sanitize_responses_input,
    uniquify_function_call_ids,
)


class TestSanitizeResponsesInput:
    """Regression guard for issue #42 -- the agent must not forward
    orphaned function_call / function_call_output items to the LLM
    or the Databricks AI Gateway returns 400 BAD_REQUEST."""

    def test_empty_list(self) -> None:
        assert sanitize_responses_input([]) == []

    def test_passes_through_messages_only(self) -> None:
        items = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        assert sanitize_responses_input(items) == items

    def test_passes_through_balanced_function_call_pair(self) -> None:
        items = [
            {"role": "user", "content": "what's 2+2?"},
            {"type": "function_call", "call_id": "c1", "name": "calc", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "c1", "output": "4"},
            {"role": "assistant", "content": "4"},
        ]
        assert sanitize_responses_input(items) == items

    def test_drops_orphan_function_call_output(self) -> None:
        items = [
            {"role": "user", "content": "what's 2+2?"},
            {"type": "function_call_output", "call_id": "c1", "output": "4"},
            {"role": "assistant", "content": "4"},
        ]
        assert sanitize_responses_input(items) == [
            {"role": "user", "content": "what's 2+2?"},
            {"role": "assistant", "content": "4"},
        ]

    def test_drops_orphan_function_call(self) -> None:
        items = [
            {"role": "user", "content": "what's 2+2?"},
            {"type": "function_call", "call_id": "c1", "name": "calc", "arguments": "{}"},
            {"role": "assistant", "content": "I'll calculate that."},
        ]
        assert sanitize_responses_input(items) == [
            {"role": "user", "content": "what's 2+2?"},
            {"role": "assistant", "content": "I'll calculate that."},
        ]

    def test_unknown_mcp_approval_items_pass_through(self) -> None:
        # Approval gating was removed; legacy approval items become
        # unknown types and must flow through the sanitizer untouched
        # (no pairing rule exists for them anymore).
        items: list[dict[str, Any]] = [
            {
                "type": "mcp_approval_request",
                "id": "ar1",
                "name": "tool",
                "arguments": "{}",
                "server_label": "srv",
            },
            {
                "type": "mcp_approval_response",
                "id": "resp1",
                "approval_request_id": "ar1",
                "approve": True,
            },
        ]
        assert sanitize_responses_input(items) == items

    def test_preserves_order_with_mixed_orphans(self) -> None:
        items = [
            {"role": "user", "content": "first"},
            {"type": "function_call", "call_id": "c1", "name": "t", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "c1", "output": "ok"},
            {"type": "function_call_output", "call_id": "orphan", "output": "??"},
            {"type": "function_call", "call_id": "c2", "name": "t", "arguments": "{}"},
            {"role": "user", "content": "second"},
        ]
        assert sanitize_responses_input(items) == [
            {"role": "user", "content": "first"},
            {"type": "function_call", "call_id": "c1", "name": "t", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "c1", "output": "ok"},
            {"role": "user", "content": "second"},
        ]

    def test_unknown_item_type_passes_through(self) -> None:
        items: list[dict[str, Any]] = [
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": "..."}]},
            {"role": "user", "content": "hi"},
        ]
        assert sanitize_responses_input(items) == items

    def test_drops_null_status_on_message_items(self) -> None:
        # ``ResponsesAgentRequest`` fills ``status`` with ``None`` for history
        # items that carry none; strict SDK converters reject that value.
        items: list[dict[str, Any]] = [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "confirm?"}],
                "status": None,
            },
        ]
        assert sanitize_responses_input(items) == [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "confirm?"}],
            },
        ]

    def test_keeps_populated_status_on_message_items(self) -> None:
        items: list[dict[str, Any]] = [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "done"}],
                "status": "completed",
            },
        ]
        assert sanitize_responses_input(items) == items

    def test_keeps_null_status_on_non_message_items(self) -> None:
        # Only ``message`` items are normalized; other item types keep their
        # payload verbatim so no unrelated pairing field is disturbed.
        items: list[dict[str, Any]] = [
            {"type": "function_call", "call_id": "c1", "name": "t", "arguments": "{}", "status": None},
            {"type": "function_call_output", "call_id": "c1", "output": "ok"},
        ]
        assert sanitize_responses_input(items) == items

    def test_no_warning_when_balanced(self, caplog: pytest.LogCaptureFixture) -> None:
        items = [
            {"role": "user", "content": "x"},
            {"type": "function_call", "call_id": "c1", "name": "t", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "c1", "output": "ok"},
        ]
        with caplog.at_level(logging.WARNING, logger="payloads.agent.runtime.responses_input"):
            sanitize_responses_input(items)
        assert not [r for r in caplog.records if "[sanitize]" in r.getMessage()]

    def test_warning_reports_counts_and_sample_ids(self, caplog: pytest.LogCaptureFixture) -> None:
        items = [
            {"type": "function_call", "call_id": "orphan_call", "name": "t", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "orphan_result", "output": "?"},
            {"type": "function_call", "call_id": "ok", "name": "t", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "ok", "output": "ok"},
        ]
        with caplog.at_level(logging.WARNING, logger="payloads.agent.runtime.responses_input"):
            sanitize_responses_input(items)

        sanitize_logs = [r for r in caplog.records if "[sanitize]" in r.getMessage()]
        assert len(sanitize_logs) == 1
        message = sanitize_logs[0].getMessage()
        assert "1 orphan tool-call(s)" in message
        assert "1 orphan tool-result(s)" in message
        assert "orphan_call" in message
        assert "orphan_result" in message
        assert "'ok'" not in message  # the paired call_id must not be reported as orphan


class TestUniquifyFunctionCallIds:
    """Regression guard for the OpenAI Agents SDK's input-item dedupe pass.

    The SDK's ``deduplicate_input_items_preferring_latest``
    (``agents/run_internal/items.py``) keys ``function_call`` items on
    ``f"id:function_call:{id}"``.  When an MLflow ``ResponsesAgent``
    emits multiple tool calls in a single assistant turn, every
    resulting ``function_call`` item shares the assistant turn's
    Responses-level ``id`` -- so the SDK silently collapses them and
    the model receives orphan ``tool_result`` blocks.

    These tests pin the contract:

    1. Multiple ``function_call`` items sharing an ``id`` get fresh
       per-item ``id`` values; ``call_id`` is never touched.
    2. Single ``function_call`` items pass through untouched.
    3. Non-``function_call`` items pass through untouched.
    4. The input list is never mutated; identity is preserved for
       items that don't need rewriting.
    5. The rewrite is logged at WARNING with the affected ``call_id``s.
    """

    def _shared_id(self) -> str:
        return "rs_abc_assistant_turn"

    def test_empty_list(self) -> None:
        assert uniquify_function_call_ids([]) == []

    def test_passes_through_messages_and_outputs(self) -> None:
        items: list[dict[str, Any]] = [
            {"role": "user", "content": "hi"},
            {"type": "function_call_output", "call_id": "c1", "output": "ok"},
            {"type": "reasoning", "id": "rs_x", "summary": []},
        ]
        assert uniquify_function_call_ids(items) == items

    def test_single_function_call_passes_through(self) -> None:
        items: list[dict[str, Any]] = [
            {
                "type": "function_call",
                "id": "rs_1",
                "call_id": "c1",
                "name": "t",
                "arguments": "{}",
            },
        ]
        assert uniquify_function_call_ids(items) == items

    def test_distinct_ids_pass_through(self) -> None:
        items: list[dict[str, Any]] = [
            {"type": "function_call", "id": "rs_1", "call_id": "c1", "name": "t", "arguments": "{}"},
            {"type": "function_call", "id": "rs_2", "call_id": "c2", "name": "t", "arguments": "{}"},
        ]
        assert uniquify_function_call_ids(items) == items

    def test_rewrites_duplicate_ids(self) -> None:
        shared = self._shared_id()
        items: list[dict[str, Any]] = [
            {"type": "function_call", "id": shared, "call_id": "c1", "name": "t", "arguments": "{}"},
            {"type": "function_call", "id": shared, "call_id": "c2", "name": "t", "arguments": "{}"},
            {"type": "function_call", "id": shared, "call_id": "c3", "name": "t", "arguments": "{}"},
        ]
        out = uniquify_function_call_ids(items)
        ids = [item["id"] for item in out]
        assert ids[0] == shared, "first occurrence must keep its original id"
        assert len(set(ids)) == 3, "all function_call ids must be unique after rewrite"
        # ``call_id`` -- the field the chat-completions converter and
        # Anthropic / Bedrock pair on -- must be untouched.
        assert [item["call_id"] for item in out] == ["c1", "c2", "c3"]
        # name/arguments must be preserved verbatim.
        for original, rewritten in zip(items, out, strict=True):
            for key in ("name", "arguments", "call_id", "type"):
                assert rewritten[key] == original[key]

    def test_production_shape_matches_observed_failure(self) -> None:
        """Mirror the exact shape observed in the diagnostic log: three
        function_calls sharing an ``id``, followed by their three paired
        function_call_outputs, then a follow-up user turn."""
        shared = self._shared_id()
        items: list[dict[str, Any]] = [
            {"role": "user", "content": "list and query"},
            {"type": "function_call", "id": shared, "call_id": "c1", "name": "list", "arguments": "{}"},
            {"type": "function_call", "id": shared, "call_id": "c2", "name": "list", "arguments": "{}"},
            {"type": "function_call", "id": shared, "call_id": "c3", "name": "exec", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "c1", "output": "..."},
            {"type": "function_call_output", "call_id": "c2", "output": "..."},
            {"type": "function_call_output", "call_id": "c3", "output": "..."},
            {"role": "user", "content": "follow up"},
        ]
        out = uniquify_function_call_ids(items)
        function_calls = [m for m in out if m.get("type") == "function_call"]
        unique_ids = {m["id"] for m in function_calls}
        assert len(function_calls) == 3, "no function_calls should be dropped"
        assert len(unique_ids) == 3, "all three function_calls must end up with distinct ids"
        # Order and surrounding items unchanged.
        assert out[0] == items[0]
        assert out[-1] == items[-1]
        assert [m.get("type") for m in out] == [m.get("type") for m in items]

    def test_does_not_mutate_input(self) -> None:
        shared = self._shared_id()
        items: list[dict[str, Any]] = [
            {"type": "function_call", "id": shared, "call_id": "c1", "name": "t", "arguments": "{}"},
            {"type": "function_call", "id": shared, "call_id": "c2", "name": "t", "arguments": "{}"},
        ]
        snapshot = [dict(item) for item in items]
        uniquify_function_call_ids(items)
        assert items == snapshot, "input list/items must not be mutated"

    def test_missing_id_passes_through(self) -> None:
        # No ``id`` field -> no collision possible -> nothing to do.
        items: list[dict[str, Any]] = [
            {"type": "function_call", "call_id": "c1", "name": "t", "arguments": "{}"},
            {"type": "function_call", "call_id": "c2", "name": "t", "arguments": "{}"},
        ]
        assert uniquify_function_call_ids(items) == items

    def test_logs_warning_with_call_ids(self, caplog: pytest.LogCaptureFixture) -> None:
        shared = self._shared_id()
        items: list[dict[str, Any]] = [
            {"type": "function_call", "id": shared, "call_id": "c1", "name": "t", "arguments": "{}"},
            {"type": "function_call", "id": shared, "call_id": "c2", "name": "t", "arguments": "{}"},
        ]
        with caplog.at_level(logging.WARNING, logger="payloads.agent.runtime.responses_input"):
            uniquify_function_call_ids(items)
        sanitize_logs = [r for r in caplog.records if "[sanitize]" in r.getMessage()]
        assert len(sanitize_logs) == 1
        message = sanitize_logs[0].getMessage()
        assert "rewrote 1 duplicate function_call id" in message
        assert "c2" in message  # the rewritten item's call_id is surfaced

    def test_no_warning_when_no_duplicates(self, caplog: pytest.LogCaptureFixture) -> None:
        items: list[dict[str, Any]] = [
            {"type": "function_call", "id": "rs_1", "call_id": "c1", "name": "t", "arguments": "{}"},
            {"type": "function_call", "id": "rs_2", "call_id": "c2", "name": "t", "arguments": "{}"},
        ]
        with caplog.at_level(logging.WARNING, logger="payloads.agent.runtime.responses_input"):
            uniquify_function_call_ids(items)
        assert not [r for r in caplog.records if "[sanitize]" in r.getMessage()]


def _tool_output(output: Any) -> dict[str, Any]:
    return {"type": "function_call_output", "call_id": "toolu_1", "output": output}


class TestRestoreToolOutputLists:
    MCP_RESULT: Any = [{"type": "input_text", "text": '{"columns":["catalog"],"rows":[["samples"]]}'}]

    def test_a_json_encoded_content_list_is_restored(self) -> None:

        items = [_tool_output(json.dumps(self.MCP_RESULT))]
        assert restore_tool_output_lists(items) == [_tool_output(self.MCP_RESULT)]

    def test_the_model_sees_what_it_saw_in_the_run(self) -> None:

        from agents.models.chatcmpl_converter import Converter

        call: Any = {"type": "function_call", "call_id": "toolu_1", "name": "sql", "arguments": "{}"}
        run_items: Any = [call, _tool_output(self.MCP_RESULT)]
        history_items: Any = restore_tool_output_lists([call, _tool_output(json.dumps(self.MCP_RESULT))])
        in_run = Converter.items_to_messages(run_items)
        from_history = Converter.items_to_messages(history_items)
        assert from_history == in_run

    @pytest.mark.parametrize(
        "output",
        [
            "42",
            "plain text",
            "[not json",
            "[]",
            '[{"columns": ["catalog"]}]',
            '[{"type": "text", "text": "x"}]',
            '[{"type": "input_text", "text": "x"}, "loose"]',
        ],
    )
    def test_other_outputs_pass_through(self, output: str) -> None:
        item = _tool_output(output)
        assert restore_tool_output_lists([item]) == [item]

    def test_other_items_and_the_input_are_untouched(self) -> None:

        message: Any = {"type": "message", "role": "user", "content": json.dumps(self.MCP_RESULT)}
        item = _tool_output(json.dumps(self.MCP_RESULT))
        restored = restore_tool_output_lists([message, item])
        assert restored[0] is message
        assert isinstance(item["output"], str)
