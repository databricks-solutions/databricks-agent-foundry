"""Tests for ``ensure_instance_on_sys_path`` -- making instance-shipped
Python importable at runtime so ``type: python`` tool ``callable_path``s
(and reset/finalize hooks) resolve.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent_foundry.contracts.tools import PythonFunctionTool
from payloads.agent.runtime import _tool_factory, instance_paths, runtime_config


class TestEnsureInstanceOnSysPath:
    def test_adds_instance_dir(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        restore_sys_state: None,
        make_instance: Callable[[Path], Path],
    ) -> None:
        instance = make_instance(tmp_path)
        monkeypatch.setenv("INSTANCE_DIR", str(instance))
        instance_paths.ensure_instance_on_sys_path()
        assert str(instance) in sys.path

    def test_idempotent(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        restore_sys_state: None,
        make_instance: Callable[[Path], Path],
    ) -> None:
        instance = make_instance(tmp_path)
        monkeypatch.setenv("INSTANCE_DIR", str(instance))
        instance_paths.ensure_instance_on_sys_path()
        instance_paths.ensure_instance_on_sys_path()
        assert sys.path.count(str(instance)) == 1

    def test_missing_instance_is_noop(self, monkeypatch: pytest.MonkeyPatch, restore_sys_state: None) -> None:
        # No INSTANCE_DIR and no marker up the tree from a temp CWD -> None.
        monkeypatch.setattr(instance_paths, "resolve_instance_dir", lambda: None)
        before = list(sys.path)
        instance_paths.ensure_instance_on_sys_path()
        assert sys.path == before


class TestImportOfInstanceShippedCallable:
    def test_callable_under_instance_dir_builds_a_tool(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        restore_sys_state: None,
        make_instance: Callable[[Path], Path],
    ) -> None:
        """End-to-end: a callable living under ``instances/<name>/`` is
        importable by its dotted path *after the production wiring runs*,
        and a ``PythonFunctionTool`` referencing it builds a FunctionTool.
        """
        instance = make_instance(tmp_path)
        pkg = instance / "mypkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "spec_tools.py").write_text("def add_signal(name: str) -> dict:\n    return {'name': name}\n")
        monkeypatch.setenv("INSTANCE_DIR", str(instance))

        # The production seam -- not a manual sys.path hack.
        instance_paths.ensure_instance_on_sys_path()

        tool = PythonFunctionTool(
            name="add_signal",
            description="Add a signal.",
            callable_path="mypkg.spec_tools.add_signal",
        )
        function_tool = _tool_factory.create_function_tool(MagicMock(), tool)
        assert function_tool is not None
        assert function_tool.name == "add_signal"
        # Inferred from the imported callable's signature.
        assert function_tool.params_json_schema["required"] == ["name"]


class TestConfigLoadSeamCallsHelper:
    def test_uncached_config_load_ensures_sys_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The shared config-load seam (run on every entry path before
        tools are built) calls ``ensure_instance_on_sys_path``."""
        spy = MagicMock()
        monkeypatch.setattr(runtime_config, "init_agent_module", lambda: None)
        monkeypatch.setattr(runtime_config, "ensure_instance_on_sys_path", spy)
        monkeypatch.setattr(runtime_config, "resolve_instance_dir", lambda: None)
        monkeypatch.setattr(runtime_config, "load_variables", dict)
        monkeypatch.setattr(runtime_config, "resolve_deployer", lambda ws, variables: None)
        monkeypatch.setattr(runtime_config, "load_agent_config", lambda instance_dir, variables: {})
        monkeypatch.setattr(runtime_config, "register_prompt_and_model", lambda config, variables: None)
        monkeypatch.setattr(runtime_config, "configure_prompt_cache", lambda config: None)
        monkeypatch.setattr(runtime_config, "configure_tool_result_clearing", lambda config: None)

        runtime_config._load_agent_config_and_vars_uncached(MagicMock())
        spy.assert_called_once()
