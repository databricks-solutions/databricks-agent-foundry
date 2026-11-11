"""Tests for payloads/agent/runtime/context_occupancy.py and its wiring.

The UI server compacts a conversation on the occupancy the runtime reports
(#637), so the value must be the size before tool-result clearing -- the
size the next turn starts from (#672) -- and must reach ``custom_outputs``
on every path that answers.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from agents.exceptions import MaxTurnsExceeded

import payloads.agent.runtime.runtime_config as _runtime_config
from payloads.agent.runtime.context_limit import (
    COMPACTING_CONTEXT_LIMIT_NOTICE,
    CONTEXT_LIMIT_NOTICE,
    MAX_TURNS_NOTICE,
)
from payloads.agent.runtime.context_occupancy import (
    CONTEXT_WINDOW_KEY,
    OccupancyRecorder,
    context_window_outputs,
    merge_custom_outputs,
    reported_input_tokens,
)
from payloads.agent.runtime.tool_result_clearing import (
    ToolResultClearingSettings,
    build_call_model_input_filter,
    estimate_input_tokens,
    run_config_for,
)

with patch.object(_runtime_config, "init_agent_module", lambda: None):
    from payloads.agent.runtime import agent as _agent_module

agent: Any = _agent_module


@dataclass
class _FakeCallModelData:
    model_data: Any
    agent: Any = None
    context: Any = None


def _tool_call(call_id: str) -> dict[str, Any]:
    return {"type": "function_call", "call_id": call_id, "name": "sql", "arguments": "{}"}


def _tool_output(call_id: str, output: str) -> dict[str, Any]:
    return {"type": "function_call_output", "call_id": call_id, "output": output}


def _model_data(items: list[Any], instructions: str = "SYSTEM") -> Any:
    from agents.run_config import ModelInputData

    return ModelInputData(input=items, instructions=instructions)


class TestContextWindowOutputs:
    def test_none_without_a_recorded_call(self) -> None:
        assert context_window_outputs(None) is None
        assert context_window_outputs(OccupancyRecorder()) is None

    def test_carries_the_recorded_estimate(self) -> None:
        recorder = OccupancyRecorder()
        recorder.record(1234)
        assert context_window_outputs(recorder) == {CONTEXT_WINDOW_KEY: {"estimated_input_tokens": 1234}}

    def test_limit_exceeded_without_any_call(self) -> None:
        assert context_window_outputs(None, limit_exceeded=True) == {CONTEXT_WINDOW_KEY: {"limit_exceeded": True}}

    def test_prefers_the_reported_count(self) -> None:
        recorder = OccupancyRecorder()
        recorder.record(29_444)
        outputs = context_window_outputs(recorder, reported_input_tokens=25_059)
        assert outputs == {CONTEXT_WINDOW_KEY: {"estimated_input_tokens": 25_059}}

    def test_scales_the_reported_count_back_to_before_clearing(self) -> None:
        recorder = OccupancyRecorder()
        recorder.record(40_000)
        recorder.record_sent(20_000)
        assert recorder.input_tokens(18_000) == 36_000

    @pytest.mark.parametrize("reported", [None, 0])
    def test_falls_back_to_the_estimate(self, reported: int | None) -> None:
        recorder = OccupancyRecorder()
        recorder.record(1234)
        assert recorder.input_tokens(reported) == 1234


class TestReportedInputTokens:
    @staticmethod
    def _response(input_tokens: Any) -> Any:
        return SimpleNamespace(usage=SimpleNamespace(input_tokens=input_tokens))

    def test_reads_the_last_response(self) -> None:
        assert reported_input_tokens([self._response(10), self._response(25_059)]) == 25_059

    @pytest.mark.parametrize("responses", [[], [SimpleNamespace(usage=None)], [SimpleNamespace()]])
    def test_none_without_usage(self, responses: list[Any]) -> None:
        assert reported_input_tokens(responses) is None

    @pytest.mark.parametrize("tokens", [0, None, "12"])
    def test_none_for_a_missing_count(self, tokens: Any) -> None:
        assert reported_input_tokens([self._response(tokens)]) is None


class TestMergeCustomOutputs:
    def test_none_when_both_empty(self) -> None:
        assert merge_custom_outputs(None, None) is None

    def test_keeps_hook_output(self) -> None:
        assert merge_custom_outputs({"spec": 1}, {CONTEXT_WINDOW_KEY: {"x": 1}}) == {
            "spec": 1,
            CONTEXT_WINDOW_KEY: {"x": 1},
        }

    def test_framework_entry_wins_a_clash(self) -> None:
        merged = merge_custom_outputs({CONTEXT_WINDOW_KEY: "hook"}, {CONTEXT_WINDOW_KEY: {"x": 1}})
        assert merged == {CONTEXT_WINDOW_KEY: {"x": 1}}

    def test_merges_several_framework_entries(self) -> None:
        merged = merge_custom_outputs({"spec": 1}, {CONTEXT_WINDOW_KEY: {"x": 1}}, None, {"reasoning_replay": []})
        assert merged == {"spec": 1, CONTEXT_WINDOW_KEY: {"x": 1}, "reasoning_replay": []}


class TestFilterRecordsOccupancy:
    def test_records_the_size_before_clearing(self) -> None:
        items: list[Any] = []
        for index in range(1, 6):
            items.append(_tool_call(f"c{index}"))
            items.append(_tool_output(f"c{index}", "X" * 5000))
        recorder = OccupancyRecorder()
        run_filter = build_call_model_input_filter(
            ToolResultClearingSettings(keep_tool_uses=1, clear_at_least_chars=0), recorder
        )

        result = run_filter(_FakeCallModelData(_model_data(items)))

        assert recorder.estimated_input_tokens == estimate_input_tokens(items, "SYSTEM")
        assert recorder.estimated_input_tokens > estimate_input_tokens(list(result.input), "SYSTEM")
        assert recorder.sent_estimated_input_tokens == estimate_input_tokens(list(result.input), "SYSTEM")

    def test_records_with_clearing_disabled(self) -> None:
        items = [_tool_call("c1"), _tool_output("c1", "X" * 5000)]
        recorder = OccupancyRecorder()
        run_filter = build_call_model_input_filter(ToolResultClearingSettings(enabled=False), recorder)

        model_data = _model_data(items)
        assert run_filter(_FakeCallModelData(model_data)) is model_data
        assert recorder.estimated_input_tokens == estimate_input_tokens(items, "SYSTEM")
        assert recorder.sent_estimated_input_tokens == recorder.estimated_input_tokens

    def test_run_config_installed_for_recording_alone(self) -> None:
        run_config = run_config_for({"TOOL_RESULT_CLEARING_ENABLED": False}, OccupancyRecorder())
        assert run_config is not None
        assert run_config.call_model_input_filter is not None


class TestContextLimitNotice:
    """When the provider rejects an oversized prompt, the answer still says so
    in ``custom_outputs``, so the UI server can compact before the next turn."""

    REQUEST: Any = [{"role": "user", "content": "hi"}]

    @pytest.fixture(autouse=True)
    def _isolated(self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
        import mlflow

        previous = mlflow.get_tracking_uri()
        monkeypatch.delenv("MLFLOW_TRACKING_URI", raising=False)
        mlflow.set_tracking_uri(f"sqlite:///{tmp_path / 'mlflow.db'}")
        yield
        mlflow.set_tracking_uri(previous)

    @staticmethod
    def _request() -> Any:
        from mlflow.types.responses import ResponsesAgentRequest

        return ResponsesAgentRequest(input=TestContextLimitNotice.REQUEST)

    @pytest.mark.asyncio
    async def test_invoke_flags_limit_exceeded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _reject(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("prompt is too long: 210000 tokens > 200000 maximum")

        monkeypatch.setattr(agent.Runner, "run", _reject)

        response = await agent._run_invoke(object(), list(self.REQUEST), 5, self._request(), {})

        assert response.custom_outputs == {CONTEXT_WINDOW_KEY: {"limit_exceeded": True}}

    @pytest.mark.asyncio
    async def test_invoke_image_notice_is_not_a_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _reject(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("Image input is not supported for this endpoint")

        monkeypatch.setattr(agent.Runner, "run", _reject)

        response = await agent._run_invoke(object(), list(self.REQUEST), 5, self._request(), {})

        assert not response.custom_outputs

    @pytest.mark.asyncio
    async def test_stream_flags_limit_exceeded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _RejectingResult:
            async def stream_events(self) -> Any:
                raise RuntimeError("prompt is too long: 210000 tokens > 200000 maximum")
                yield  # pragma: no cover - makes this an async generator

        monkeypatch.setattr(agent.Runner, "run_streamed", lambda *_a, **_k: _RejectingResult())

        events = [e async for e in agent._run_stream(object(), list(self.REQUEST), 5, self._request(), {})]

        completed = [e for e in events if e.type == "response.completed"]
        assert len(completed) == 1
        assert completed[0].custom_outputs == {CONTEXT_WINDOW_KEY: {"limit_exceeded": True}}

    @pytest.mark.asyncio
    async def test_stream_step_limit_ends_with_a_notice(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _ExhaustedResult:
            async def stream_events(self) -> Any:
                raise MaxTurnsExceeded("Max turns (5) exceeded")
                yield  # pragma: no cover - makes this an async generator

        monkeypatch.setattr(agent.Runner, "run_streamed", lambda *_a, **_k: _ExhaustedResult())

        events = [e async for e in agent._run_stream(object(), list(self.REQUEST), 5, self._request(), {})]

        assert MAX_TURNS_NOTICE in str([e.model_dump() for e in events])

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("enabled", "expected"),
        [("true", COMPACTING_CONTEXT_LIMIT_NOTICE), ("false", CONTEXT_LIMIT_NOTICE)],
    )
    async def test_notice_follows_compaction(
        self, monkeypatch: pytest.MonkeyPatch, enabled: str, expected: str
    ) -> None:
        async def _reject(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("prompt is too long: 210000 tokens > 200000 maximum")

        monkeypatch.setattr(agent.Runner, "run", _reject)
        monkeypatch.setenv("ENABLE_COMPACTION", enabled)

        response = await agent._run_invoke(object(), list(self.REQUEST), 5, self._request(), {})

        assert response.output[0].model_dump()["content"][0]["text"] == expected
