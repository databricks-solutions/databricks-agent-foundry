"""Agent payload extensions.

An extension adds product-specific surface to the stock agent payload
without editing it: agent-server routes, chat-proxy paths for client-side
routes, and per-request tools.  Its chat-UI overlay is wired separately
through ``CHAT_UI_OVERLAYS`` because the App host must not import payload
code.

Extensions are listed in ``AGENT_EXTENSIONS`` (comma-separated module
paths), normally set by a wrapping payload adapter.  Each module exposes
``EXTENSION``, an object satisfying :class:`AgentExtension`.
"""

from __future__ import annotations

import functools
import importlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from agent_foundry import env

if TYPE_CHECKING:
    from fastapi import FastAPI

EXTENSION_ATTRIBUTE = "EXTENSION"


@dataclass(frozen=True)
class ProxyPaths:
    """Client-side routes the chat proxy must forward to the chat UI."""

    exact: tuple[str, ...] = ()
    prefixes: tuple[str, ...] = ()


@dataclass(frozen=True)
class RequestContext:
    """Per-request identity an extension tool may key state on."""

    conversation_id: str | None
    user_id: str | None
    headers: dict[str, str] = field(default_factory=dict)


@runtime_checkable
class AgentExtension(Protocol):
    """What an agent payload extension contributes."""

    name: str

    def proxy_paths(self) -> ProxyPaths:
        """Client-side routes added by the extension's UI overlay."""
        ...

    def register_routes(self, app: FastAPI) -> None:
        """Mount extension routes on the agent server, ahead of the chat proxy."""
        ...

    def request_tools(self, context: RequestContext) -> list[Any]:
        """Tools for one request; may close over *context*."""
        ...


class ExtensionLoadError(RuntimeError):
    """An ``AGENT_EXTENSIONS`` entry could not be loaded."""


def _module_paths(raw: str | None) -> list[str]:
    return [entry.strip() for entry in (raw or "").split(",") if entry.strip()]


def _load_one(module_path: str) -> AgentExtension:
    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        raise ExtensionLoadError(f"Cannot import agent extension {module_path!r}: {exc}") from exc
    extension = getattr(module, EXTENSION_ATTRIBUTE, None)
    if not isinstance(extension, AgentExtension):
        raise ExtensionLoadError(
            f"Agent extension module {module_path!r} must expose {EXTENSION_ATTRIBUTE!r} implementing AgentExtension"
        )
    return extension


@functools.cache
def load_extensions() -> tuple[AgentExtension, ...]:
    """Load every extension named in ``AGENT_EXTENSIONS``, once per process."""
    return tuple(_load_one(path) for path in _module_paths(env.read_optional(env.AGENT_EXTENSIONS)))


def merged_proxy_paths(extensions: Sequence[AgentExtension]) -> ProxyPaths:
    exact: list[str] = []
    prefixes: list[str] = []
    for extension in extensions:
        paths = extension.proxy_paths()
        exact.extend(p for p in paths.exact if p not in exact)
        prefixes.extend(p for p in paths.prefixes if p not in prefixes)
    return ProxyPaths(exact=tuple(exact), prefixes=tuple(prefixes))


def extension_tools(extensions: Sequence[AgentExtension], context: RequestContext) -> list[Any]:
    tools: list[Any] = []
    for extension in extensions:
        tools.extend(extension.request_tools(context))
    return tools


__all__ = [
    "AgentExtension",
    "ExtensionLoadError",
    "ProxyPaths",
    "RequestContext",
    "extension_tools",
    "load_extensions",
    "merged_proxy_paths",
]
