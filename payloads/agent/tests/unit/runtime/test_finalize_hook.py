"""Tests for the generic finalize / reset lifecycle hooks (Feature: custom_outputs).

These live in :mod:`payloads.agent.runtime.agent`, whose module body calls
``init_agent_module()`` (which needs Databricks auth).  We patch that to a
no-op *before* importing the module so the import neither authenticates nor
hits the network -- ``init_agent_module`` is idempotent, so this is safe even
if another test imported the module first.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

import payloads.agent.runtime.runtime_config as _runtime_config
from payloads.agent.runtime.request_context import RequestState

with patch.object(_runtime_config, "init_agent_module", lambda: None):
    from payloads.agent.runtime import agent


# ---------------------------------------------------------------------------
# Hook callables (resolved by monkeypatching agent.import_python_callable)
# ---------------------------------------------------------------------------

_RESET_CALLS: list[str] = []
_RESET_CAPTURED: list[Any] = []


def reset_hook() -> None:
    _RESET_CALLS.append("reset")


def reset_with_request(request: Any) -> None:
    _RESET_CAPTURED.append(request)


def finalize_no_args() -> dict[str, Any]:
    return {"kind": "no_args"}


def finalize_with_result(result: Any) -> dict[str, Any]:
    return {"item_count": len(result.new_items)}


def finalize_with_request_and_result(request: Any, result: Any) -> dict[str, Any]:
    return {"user": request.user, "items": len(result.new_items)}


def finalize_returns_non_dict(result: Any) -> str:
    return "not a dict"


def finalize_returns_empty(result: Any) -> dict[str, Any]:
    return {}


def finalize_raises(result: Any) -> dict[str, Any]:
    raise RuntimeError("finalize boom")


async def finalize_async(result: Any) -> dict[str, Any]:
    return {"async": True}


def reset_seeds_state(state: Any) -> None:
    # A reset hook declaring ``state`` seeds the request-scoped bag.
    state.data["seed"] = "seeded"


def finalize_reads_state(state: Any) -> dict[str, Any]:
    # A finalize hook declaring ``state`` reads back what tools/reset put there.
    return {"seen": state.data.get("seed")}


@pytest.fixture
def stub_import(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Route agent.import_python_callable through a name->callable registry."""
    registry: dict[str, Any] = {
        "hooks.reset": reset_hook,
        "hooks.reset_with_request": reset_with_request,
        "hooks.reset_with_state": reset_seeds_state,
        "hooks.no_args": finalize_no_args,
        "hooks.with_result": finalize_with_result,
        "hooks.request_result": finalize_with_request_and_result,
        "hooks.with_state": finalize_reads_state,
        "hooks.non_dict": finalize_returns_non_dict,
        "hooks.empty": finalize_returns_empty,
        "hooks.raises": finalize_raises,
        "hooks.async": finalize_async,
    }
    monkeypatch.setattr(agent, "import_python_callable", lambda path: registry[path])
    return registry


def _request(user: str = "u1") -> Any:
    return SimpleNamespace(user=user)


def _result(n: int = 0) -> Any:
    return SimpleNamespace(new_items=list(range(n)))


class TestRunReset:
    @pytest.mark.asyncio
    async def test_calls_reset_when_configured(self, stub_import: dict[str, Any]) -> None:
        """A zero-arg reset hook still runs under the new signature."""
        _RESET_CALLS.clear()
        await agent._run_reset({"RESET_CALLABLE": "hooks.reset"}, _request())
        assert _RESET_CALLS == ["reset"]

    @pytest.mark.asyncio
    async def test_reset_receives_request(self, stub_import: dict[str, Any]) -> None:
        """A reset hook declaring ``request`` receives it (symmetric with finalize)."""
        _RESET_CAPTURED.clear()
        await agent._run_reset({"RESET_CALLABLE": "hooks.reset_with_request"}, _request("alice"))
        assert len(_RESET_CAPTURED) == 1
        assert _RESET_CAPTURED[0].user == "alice"

    @pytest.mark.asyncio
    async def test_noop_when_unset(self, stub_import: dict[str, Any]) -> None:
        _RESET_CALLS.clear()
        await agent._run_reset({}, _request())
        await agent._run_reset(None, _request())
        assert _RESET_CALLS == []


class TestHookInstanceBoundary:
    """Hooks load through the same fail-closed instance-dir check as tools
    (no ``stub_import``: the real ``import_python_callable`` runs)."""

    @pytest.mark.asyncio
    async def test_reset_outside_instance_is_rejected(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        restore_sys_state: None,
        make_instance: Callable[[Path], Path],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setenv("INSTANCE_DIR", str(make_instance(tmp_path)))
        with caplog.at_level(logging.WARNING):
            # Must not raise: a rejected hook is skipped with a warning.
            await agent._run_reset({"RESET_CALLABLE": "json.dumps"}, _request(), RequestState())
        rejected = [r for r in caplog.records if "reset callable" in r.getMessage()]
        assert len(rejected) == 1
        assert rejected[0].exc_info is not None
        assert "outside the instance dir" in str(rejected[0].exc_info[1])

    @pytest.mark.asyncio
    async def test_finalize_outside_instance_returns_none(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        restore_sys_state: None,
        make_instance: Callable[[Path], Path],
    ) -> None:
        monkeypatch.setenv("INSTANCE_DIR", str(make_instance(tmp_path)))
        out = await agent._run_finalize({"FINALIZE_CALLABLE": "json.loads"}, _request(), _result(), RequestState())
        assert out is None


class TestRunFinalize:
    @pytest.mark.asyncio
    async def test_none_when_unset(self, stub_import: dict[str, Any]) -> None:
        assert await agent._run_finalize({}, _request(), _result()) is None
        assert await agent._run_finalize(None, _request(), _result()) is None

    @pytest.mark.asyncio
    async def test_zero_arg_hook(self, stub_import: dict[str, Any]) -> None:
        out = await agent._run_finalize({"FINALIZE_CALLABLE": "hooks.no_args"}, _request(), _result())
        assert out == {"kind": "no_args"}

    @pytest.mark.asyncio
    async def test_result_only_hook(self, stub_import: dict[str, Any]) -> None:
        out = await agent._run_finalize({"FINALIZE_CALLABLE": "hooks.with_result"}, _request(), _result(3))
        assert out == {"item_count": 3}

    @pytest.mark.asyncio
    async def test_request_and_result_hook(self, stub_import: dict[str, Any]) -> None:
        out = await agent._run_finalize(
            {"FINALIZE_CALLABLE": "hooks.request_result"},
            _request("alice"),
            _result(2),
        )
        assert out == {"user": "alice", "items": 2}

    @pytest.mark.asyncio
    async def test_async_hook_awaited(self, stub_import: dict[str, Any]) -> None:
        out = await agent._run_finalize({"FINALIZE_CALLABLE": "hooks.async"}, _request(), _result())
        assert out == {"async": True}

    @pytest.mark.asyncio
    async def test_non_dict_return_ignored(self, stub_import: dict[str, Any]) -> None:
        out = await agent._run_finalize({"FINALIZE_CALLABLE": "hooks.non_dict"}, _request(), _result())
        assert out is None

    @pytest.mark.asyncio
    async def test_empty_dict_collapses_to_none(self, stub_import: dict[str, Any]) -> None:
        # Keeps invoke/stream symmetric: neither delivers an empty payload.
        out = await agent._run_finalize({"FINALIZE_CALLABLE": "hooks.empty"}, _request(), _result())
        assert out is None

    @pytest.mark.asyncio
    async def test_exception_swallowed(self, stub_import: dict[str, Any]) -> None:
        out = await agent._run_finalize({"FINALIZE_CALLABLE": "hooks.raises"}, _request(), _result())
        assert out is None


class TestStateInjection:
    """The ``state`` kwarg is injected into hooks that declare it, and the
    same RequestState instance flows reset -> finalize."""

    @pytest.mark.asyncio
    async def test_reset_receives_and_seeds_state(self, stub_import: dict[str, Any]) -> None:
        state = RequestState()
        await agent._run_reset({"RESET_CALLABLE": "hooks.reset_with_state"}, _request(), state)
        assert state.data["seed"] == "seeded"

    @pytest.mark.asyncio
    async def test_finalize_reads_state(self, stub_import: dict[str, Any]) -> None:
        state = RequestState(data={"seed": "from_tool"})
        out = await agent._run_finalize({"FINALIZE_CALLABLE": "hooks.with_state"}, _request(), _result(), state)
        assert out == {"seen": "from_tool"}

    @pytest.mark.asyncio
    async def test_reset_and_finalize_share_one_instance(self, stub_import: dict[str, Any]) -> None:
        """reset seeds state.data; finalize reads it back from the SAME object."""
        state = RequestState()
        await agent._run_reset({"RESET_CALLABLE": "hooks.reset_with_state"}, _request(), state)
        out = await agent._run_finalize({"FINALIZE_CALLABLE": "hooks.with_state"}, _request(), _result(), state)
        assert out == {"seen": "seeded"}

    @pytest.mark.asyncio
    async def test_state_optional_for_request_only_hooks(self, stub_import: dict[str, Any]) -> None:
        """A hook declaring only ``request`` is unaffected when state is passed."""
        _RESET_CAPTURED.clear()
        await agent._run_reset({"RESET_CALLABLE": "hooks.reset_with_request"}, _request("bob"), RequestState())
        assert _RESET_CAPTURED[0].user == "bob"


class TestCustomOutputsEvent:
    def test_builds_completed_event_carrying_custom_outputs(self) -> None:
        event = agent._custom_outputs_event({"spec": {"n": 1}})
        assert event.type == "response.completed"
        assert event.custom_outputs == {"spec": {"n": 1}}
        # ``response`` is an extra field stored as the raw dict we passed;
        # the output items were already streamed as their own events, so
        # this envelope carries no output of its own.
        assert getattr(event, "response")["output"] == []
