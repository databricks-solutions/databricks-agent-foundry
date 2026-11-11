"""Agent brick test fixtures."""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from payloads.agent.runtime import mcp_tools_cache
from payloads.agent.runtime.request_context import _environment_workspace_client

BRICK_ROOT = Path(__file__).resolve().parents[3] / "runtime"
PROJECT_ROOT = BRICK_ROOT.parent.parent

ALICE = "1234567890@5678"
CHAT = "chat-1"


@pytest.fixture(autouse=True)
def _clear_mcp_tools_cache() -> Iterator[None]:
    """Keep the process-wide MCP tools-list cache from leaking across tests."""
    mcp_tools_cache.clear()
    yield
    mcp_tools_cache.clear()


@pytest.fixture(autouse=True)
def _clear_environment_workspace_client() -> Iterator[None]:
    """Keep the process-wide service-principal client from leaking across tests."""
    _environment_workspace_client.cache_clear()
    yield
    _environment_workspace_client.cache_clear()


@pytest.fixture
def restore_sys_state() -> Iterator[None]:
    """Snapshot ``sys.path`` / ``sys.modules`` and restore them after the
    test, so a fixture instance dir and any modules imported from it never
    leak into other tests."""
    saved_path = list(sys.path)
    saved_modules = set(sys.modules)
    try:
        yield
    finally:
        sys.path[:] = saved_path
        for name in set(sys.modules) - saved_modules:
            del sys.modules[name]


@pytest.fixture
def make_instance() -> Callable[[Path], Path]:
    """Factory creating a minimal instance dir (``foundry.toml`` marker)."""

    def _make(tmp_path: Path) -> Path:
        instance = tmp_path / "my-instance"
        instance.mkdir()
        (instance / "foundry.toml").write_text("")
        return instance

    return _make
