"""Tests for the in-process ``type: python`` function tool.

Covers the three layers the feature spans:

* the low-level factory / introspection helpers in
  :mod:`payloads.agent.runtime.tools`
  (``import_python_callable`` / ``_infer_params_schema`` /
  ``create_python_function_tool``),
* the shared dispatch arm in
  :mod:`payloads.agent.runtime._tool_factory`, and
* the ``mcp_only``-independent build path
  (:func:`payloads.agent.runtime.tool_builders.create_python_tools`) plus parity
  with the two general factories.

Callables are resolved by monkeypatching ``import_python_callable`` (or
passed directly) rather than by real dotted-path import, so the tests do
not depend on the pytest import mode making this module importable by name.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from agent_foundry.contracts.tools import PythonFunctionTool
from payloads.agent.runtime import _tool_factory, instance_paths
from payloads.agent.runtime import tools as tools_mod
from payloads.agent.runtime.tool_builders import create_python_tools, create_skill_tools, create_tools
from payloads.agent.runtime.tools import (
    _infer_params_schema,
    create_python_function_tool,
    import_python_callable,
)

# ---------------------------------------------------------------------------
# Sample callables (targets for the factory / dispatch tests)
# ---------------------------------------------------------------------------


def add_signal(name: str, weight: float, active: bool, note: str = "x") -> dict[str, Any]:
    return {"name": name, "weight": weight, "active": active, "note": note}


def returns_str(x: str) -> str:
    return f"got {x}"


def boom(x: str) -> str:
    raise RuntimeError("kaboom")


def with_ctx_arg(ctx: Any, value: int) -> dict[str, Any]:
    # ``ctx`` no longer opts into the run state: it is an ordinary LLM argument.
    return {"ctx": ctx, "value": value}


def with_state(state: Any, value: int) -> dict[str, Any]:
    # Leading ``state`` param opts into the bare RequestState; accumulate on it.
    state.data.setdefault("values", []).append(value)
    return {"accumulated": state.data["values"]}


async def async_tool(value: int) -> dict[str, int]:
    return {"doubled": value * 2}


# ---------------------------------------------------------------------------
# import_python_callable
# ---------------------------------------------------------------------------


@pytest.fixture
def instance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    restore_sys_state: None,
    make_instance: Callable[[Path], Path],
) -> Iterator[Path]:
    """An active instance dir on ``sys.path`` (the production wiring), with a
    fresh ContextVar-warning registry so warnings are observable per test."""
    inst = make_instance(tmp_path)
    monkeypatch.setenv("INSTANCE_DIR", str(inst))
    monkeypatch.setattr(tools_mod, "_CONTEXTVAR_CHECKED", set())
    instance_paths.ensure_instance_on_sys_path()
    yield inst


def _write(root: Path, rel: str, code: str) -> None:
    """Write *code* to ``root/rel``, creating packages (``__init__.py``) on the way."""
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    for parent in target.relative_to(root).parents:
        if str(parent) != ".":
            (root / parent / "__init__.py").touch()
    target.write_text(code)


class TestImportPythonCallable:
    """``import_python_callable`` is fail-closed to the active instance dir."""

    def test_imports_instance_callable(self, instance: Path) -> None:
        _write(instance, "ipkg_ok/mod.py", "def f():\n    return 'ok'\n")
        fn = import_python_callable("ipkg_ok.mod.f")
        assert fn() == "ok"

    def test_stdlib_callable_rejected(self, instance: Path) -> None:
        with pytest.raises(ValueError, match="outside the instance dir"):
            import_python_callable("json.dumps")

    def test_reexport_from_non_instance_module_rejected(self, instance: Path) -> None:
        _write(instance, "ipkg_reexp/mod.py", "from json import dumps\n")
        with pytest.raises(ValueError, match="defined outside the instance dir"):
            import_python_callable("ipkg_reexp.mod.dumps")

    def test_shadowed_instance_package_rejected_without_executing(self, instance: Path, tmp_path: Path) -> None:
        # An outside package of the same name earlier on sys.path (like an
        # installed one) wins resolution; it must be refused before it runs.
        sentinel = tmp_path / "executed"
        outside = tmp_path / "outside"
        _write(outside, "ipkg_shadow/mod.py", "def f():\n    pass\n")
        (outside / "ipkg_shadow" / "__init__.py").write_text(f"open({str(sentinel)!r}, 'w').close()\n")
        _write(instance, "ipkg_shadow/mod.py", "def f():\n    pass\n")
        sys.path.insert(0, str(outside))
        with pytest.raises(ValueError, match="outside the instance dir"):
            import_python_callable("ipkg_shadow.mod.f")
        assert not sentinel.exists()

    def test_outside_module_not_executed(self, instance: Path, tmp_path: Path) -> None:
        sentinel = tmp_path / "executed"
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "ioutside_mod.py").write_text(f"open({str(sentinel)!r}, 'w').close()\ndef f():\n    pass\n")
        sys.path.append(str(outside))
        with pytest.raises(ValueError, match="outside the instance dir"):
            import_python_callable("ioutside_mod.f")
        assert not sentinel.exists()

    def test_no_instance_dir_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(tools_mod, "resolve_instance_dir", lambda: None)
        with pytest.raises(ValueError, match="no active instance dir"):
            import_python_callable("json.dumps")

    def test_missing_module_part_raises(self, instance: Path) -> None:
        with pytest.raises(ValueError, match="module.attr"):
            import_python_callable("dumps")

    def test_missing_attr_raises(self, instance: Path) -> None:
        _write(instance, "ipkg_attr/mod.py", "X = 1\n")
        with pytest.raises(ValueError, match="not callable"):
            import_python_callable("ipkg_attr.mod.not_there")

    def test_non_callable_attr_raises(self, instance: Path) -> None:
        _write(instance, "ipkg_attr2/mod.py", "X = 1\n")
        with pytest.raises(ValueError, match="not callable"):
            import_python_callable("ipkg_attr2.mod.X")

    def test_bad_module_raises_import_error(self, instance: Path) -> None:
        with pytest.raises(ImportError):
            import_python_callable("no_such_module_xyz.func")

    def test_module_level_contextvar_warns_once(self, instance: Path, caplog: pytest.LogCaptureFixture) -> None:
        _write(
            instance,
            "ipkg_cv/mod.py",
            "import contextvars\nACC = contextvars.ContextVar('acc')\ndef f():\n    pass\n",
        )
        with caplog.at_level(logging.WARNING, logger=tools_mod.logger.name):
            import_python_callable("ipkg_cv.mod.f")
            import_python_callable("ipkg_cv.mod.f")
        warnings = [r for r in caplog.records if "ContextVar" in r.getMessage()]
        assert len(warnings) == 1
        assert "ACC" in warnings[0].getMessage()

    def test_plain_module_does_not_warn(self, instance: Path, caplog: pytest.LogCaptureFixture) -> None:
        _write(instance, "ipkg_plain/mod.py", "def f():\n    pass\n")
        with caplog.at_level(logging.WARNING, logger=tools_mod.logger.name):
            import_python_callable("ipkg_plain.mod.f")
        assert not [r for r in caplog.records if "ContextVar" in r.getMessage()]


# ---------------------------------------------------------------------------
# _infer_params_schema
# ---------------------------------------------------------------------------


class TestInferParamsSchema:
    def test_primitives_and_required(self) -> None:
        schema = _infer_params_schema(add_signal)
        assert schema["type"] == "object"
        assert schema["properties"] == {
            "name": {"type": "string"},
            "weight": {"type": "number"},
            "active": {"type": "boolean"},
            "note": {"type": "string"},
        }
        # ``note`` has a default, so it is not required.
        assert schema["required"] == ["name", "weight", "active"]

    def test_no_required_key_when_all_defaulted(self) -> None:
        def f(a: int = 1) -> int:
            return a

        schema = _infer_params_schema(f)
        assert "required" not in schema

    def test_leading_ctx_param_is_ordinary_llm_arg(self) -> None:
        # Only ``state`` opts into the run state; ``ctx`` stays in the schema.
        schema = _infer_params_schema(with_ctx_arg)
        assert schema["properties"] == {"ctx": {"type": "string"}, "value": {"type": "integer"}}
        assert schema["required"] == ["ctx", "value"]

    def test_leading_state_param_skipped(self) -> None:
        schema = _infer_params_schema(with_state)
        assert "state" not in schema["properties"]
        assert schema["properties"] == {"value": {"type": "integer"}}
        assert schema["required"] == ["value"]

    def test_unannotated_defaults_to_string(self) -> None:
        def f(a: object) -> Any:
            return a

        schema = _infer_params_schema(f)
        assert schema["properties"] == {"a": {"type": "string"}}


# ---------------------------------------------------------------------------
# create_python_function_tool (invocation)
# ---------------------------------------------------------------------------


class TestCreatePythonFunctionTool:
    @pytest.mark.asyncio
    async def test_dict_result_is_json_encoded(self) -> None:
        tool = create_python_function_tool("add", "d", add_signal, _infer_params_schema(add_signal))
        out = await tool.on_invoke_tool(cast(Any, None), json.dumps({"name": "s", "weight": 1.5, "active": True}))
        assert json.loads(out) == {"name": "s", "weight": 1.5, "active": True, "note": "x"}

    @pytest.mark.asyncio
    async def test_str_result_passed_through(self) -> None:
        tool = create_python_function_tool("rs", "d", returns_str, _infer_params_schema(returns_str))
        out = await tool.on_invoke_tool(cast(Any, None), json.dumps({"x": "hi"}))
        assert out == "got hi"

    @pytest.mark.asyncio
    async def test_exception_fails_soft_as_error_string(self) -> None:
        tool = create_python_function_tool("boom", "d", boom, _infer_params_schema(boom))
        out = await tool.on_invoke_tool(cast(Any, None), json.dumps({"x": "hi"}))
        assert out.startswith("Tool 'boom' failed:")
        assert "kaboom" in out

    @pytest.mark.asyncio
    async def test_bare_state_fallback_without_wrapper(self) -> None:
        # Defensive fallback: when the first arg has no ``.context`` (a bare
        # state in a direct unit call), the callable receives that object.
        state = SimpleNamespace(data={})
        tool = create_python_function_tool("ws", "d", with_state, _infer_params_schema(with_state))
        out = await tool.on_invoke_tool(cast(Any, state), json.dumps({"value": 7}))
        assert json.loads(out) == {"accumulated": [7]}

    @pytest.mark.asyncio
    async def test_ctx_named_param_gets_llm_value_not_state(self) -> None:
        tool = create_python_function_tool("wc", "d", with_ctx_arg, _infer_params_schema(with_ctx_arg))
        wrapper = SimpleNamespace(context=SimpleNamespace(data={}))
        out = await tool.on_invoke_tool(cast(Any, wrapper), json.dumps({"ctx": "hello", "value": 7}))
        assert json.loads(out) == {"ctx": "hello", "value": 7}

    @pytest.mark.asyncio
    async def test_bare_state_unwrapped_from_wrapper(self) -> None:
        # On a real run the first arg is the SDK RunContextWrapper; the
        # callable must receive the *bare* RequestState (its ``.context``),
        # not the wrapper.
        state = SimpleNamespace(data={})
        wrapper = SimpleNamespace(context=state)
        tool = create_python_function_tool("ws", "d", with_state, _infer_params_schema(with_state))
        out = await tool.on_invoke_tool(cast(Any, wrapper), json.dumps({"value": 1}))
        assert json.loads(out) == {"accumulated": [1]}
        # Mutation landed on the bare state object, proving unwrap.
        assert state.data["values"] == [1]

    @pytest.mark.asyncio
    async def test_state_accumulates_across_calls_on_shared_object(self) -> None:
        # The same context instance is shared across tool calls in one run,
        # so successive invocations accumulate on it.
        state = SimpleNamespace(data={})
        wrapper = SimpleNamespace(context=state)
        tool = create_python_function_tool("ws", "d", with_state, _infer_params_schema(with_state))
        await tool.on_invoke_tool(cast(Any, wrapper), json.dumps({"value": 1}))
        out = await tool.on_invoke_tool(cast(Any, wrapper), json.dumps({"value": 2}))
        assert json.loads(out) == {"accumulated": [1, 2]}

    @pytest.mark.asyncio
    async def test_none_ctx_tolerated_for_state_tool(self) -> None:
        # A state-declaring tool invoked with ctx=None gets None and fails
        # soft (no crash) rather than raising out of the run.
        tool = create_python_function_tool("ws", "d", with_state, _infer_params_schema(with_state))
        out = await tool.on_invoke_tool(cast(Any, None), json.dumps({"value": 1}))
        assert out.startswith("Tool 'ws' failed:")

    @pytest.mark.asyncio
    async def test_async_callable_awaited(self) -> None:
        tool = create_python_function_tool("at", "d", async_tool, _infer_params_schema(async_tool))
        out = await tool.on_invoke_tool(cast(Any, None), json.dumps({"value": 21}))
        assert json.loads(out) == {"doubled": 42}

    @pytest.mark.asyncio
    async def test_empty_args_json(self) -> None:
        def f() -> str:
            return "ok"

        tool = create_python_function_tool("f", "d", f, {"type": "object", "properties": {}})
        assert await tool.on_invoke_tool(cast(Any, None), "") == "ok"


# ---------------------------------------------------------------------------
# Dispatch via create_function_tool
# ---------------------------------------------------------------------------


class TestDispatch:
    def test_builds_tool_with_inferred_schema(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_tool_factory, "import_python_callable", lambda path: add_signal)
        tool = PythonFunctionTool(name="add", description="Add a signal", callable_path="pkg.mod.add_signal")
        ft = _tool_factory.create_function_tool(MagicMock(), tool)
        assert ft is not None
        assert ft.name == "add"
        # Inferred from the callable signature.
        assert ft.params_json_schema["required"] == ["name", "weight", "active"]

    def test_explicit_schema_honored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_tool_factory, "import_python_callable", lambda path: add_signal)
        explicit = {"type": "object", "properties": {"only": {"type": "string"}}}
        tool = PythonFunctionTool(
            name="add",
            description="d",
            callable_path="pkg.mod.add_signal",
            params_json_schema=explicit,
        )
        ft = _tool_factory.create_function_tool(MagicMock(), tool)
        assert ft is not None
        assert ft.params_json_schema == explicit

    def test_bad_callable_path_fails_soft(self) -> None:
        tool = PythonFunctionTool(name="x", description="d", callable_path="no_such_module_xyz.func")
        assert _tool_factory.create_function_tool(MagicMock(), tool) is None


# ---------------------------------------------------------------------------
# create_python_tools (mcp_only-independent build) + parity
# ---------------------------------------------------------------------------


@pytest.fixture
def _stub_resolve_and_import(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the ws-client resolution and dotted-path import both paths use."""
    monkeypatch.setattr("payloads.agent.runtime.tool_builders.resolve_tools_client", lambda ws: ws)
    monkeypatch.setattr(_tool_factory, "import_python_callable", lambda path: add_signal)


def _python_dict(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "type": "python",
        "description": "Add a signal.",
        "callable_path": "pkg.mod.add_signal",
    }


class TestCreatePythonTools:
    def test_builds_from_both_config_keys(self, _stub_resolve_and_import: None) -> None:
        config = {
            "TOOLS": [_python_dict("agent_tool")],
            "SKILL_TOOLS": [_python_dict("skill_tool")],
        }
        tools = create_python_tools(MagicMock(), config)
        assert sorted(t.name for t in tools) == ["agent_tool", "skill_tool"]

    def test_dedupes_by_name_agent_wins(self, _stub_resolve_and_import: None) -> None:
        config = {
            "TOOLS": [_python_dict("dup")],
            "SKILL_TOOLS": [_python_dict("dup")],
        }
        tools = create_python_tools(MagicMock(), config)
        assert [t.name for t in tools] == ["dup"]

    def test_ignores_non_python_types(self, _stub_resolve_and_import: None) -> None:
        config = {
            "SKILL_TOOLS": [
                {"name": "g", "type": "genie", "description": "d", "space_id": "s1"},
                _python_dict("py"),
            ],
        }
        tools = create_python_tools(MagicMock(), config)
        assert [t.name for t in tools] == ["py"]

    def test_empty_config(self, _stub_resolve_and_import: None) -> None:
        assert create_python_tools(MagicMock(), {}) == []


class TestParityAcrossEntryPoints:
    def test_both_entry_points_build_python_tool(self, _stub_resolve_and_import: None) -> None:
        skill_tools = create_skill_tools(MagicMock(), {"SKILL_TOOLS": [_python_dict("p")]}, {})
        agent_tools = create_tools(MagicMock(), {"TOOLS": [_python_dict("p")]}, {})
        assert [t.name for t in skill_tools] == ["p"]
        assert [t.name for t in agent_tools] == ["p"]
