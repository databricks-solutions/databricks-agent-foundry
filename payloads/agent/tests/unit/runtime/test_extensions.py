"""Agent payload extensions: loading, merging and per-request tools."""

from __future__ import annotations

import sys
import types
from collections.abc import Iterator
from typing import Any

import pytest

from payloads.agent.runtime import extensions as ext


class _FakeExtension:
    def __init__(self, name: str, *, exact: tuple[str, ...] = ()) -> None:
        self.name = name
        self._exact = exact
        self.seen: list[ext.RequestContext] = []

    def proxy_paths(self) -> ext.ProxyPaths:
        return ext.ProxyPaths(exact=self._exact, prefixes=tuple(f"{p}/" for p in self._exact))

    def register_routes(self, app: Any) -> None:
        app.registered.append(self.name)

    def request_tools(self, context: ext.RequestContext) -> list[Any]:
        self.seen.append(context)
        return [f"{self.name}-tool"]


@pytest.fixture
def fake_module(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    module = types.ModuleType("fake_agent_extension")
    module.EXTENSION = _FakeExtension("fake", exact=("/fake",))  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fake_agent_extension", module)
    ext.load_extensions.cache_clear()
    yield "fake_agent_extension"
    ext.load_extensions.cache_clear()


def test_no_env_means_no_extensions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_EXTENSIONS", raising=False)
    ext.load_extensions.cache_clear()
    assert ext.load_extensions() == ()


def test_loads_extension_from_env(monkeypatch: pytest.MonkeyPatch, fake_module: str) -> None:
    monkeypatch.setenv("AGENT_EXTENSIONS", f" {fake_module} , ")
    loaded = ext.load_extensions()
    assert [e.name for e in loaded] == ["fake"]


def test_missing_module_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_EXTENSIONS", "no_such_agent_extension_module")
    ext.load_extensions.cache_clear()
    with pytest.raises(ext.ExtensionLoadError, match="Cannot import"):
        ext.load_extensions()
    ext.load_extensions.cache_clear()


def test_module_without_extension_attribute_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "empty_agent_extension", types.ModuleType("empty_agent_extension"))
    monkeypatch.setenv("AGENT_EXTENSIONS", "empty_agent_extension")
    ext.load_extensions.cache_clear()
    with pytest.raises(ext.ExtensionLoadError, match="EXTENSION"):
        ext.load_extensions()
    ext.load_extensions.cache_clear()


def test_merged_proxy_paths_dedupes_in_order() -> None:
    merged = ext.merged_proxy_paths([_FakeExtension("a", exact=("/a", "/b")), _FakeExtension("b", exact=("/b", "/c"))])
    assert merged.exact == ("/a", "/b", "/c")
    assert merged.prefixes == ("/a/", "/b/", "/c/")


def test_extension_tools_receive_request_context() -> None:
    first, second = _FakeExtension("a"), _FakeExtension("b")
    context = ext.RequestContext(conversation_id="c1", user_id="u@example.com")
    assert ext.extension_tools([first, second], context) == ["a-tool", "b-tool"]
    assert first.seen == [context]
