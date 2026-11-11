"""MCP servers that are configured but cannot serve the current request.

Collected while the request's servers are built and connected, so the
model, the user and the trace can be told which tools are missing.
Reasons are fixed codes, never exception text: errors can carry URLs or
upstream response bodies.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

UnavailableReason = Literal["invalid_state", "no_user_token", "tool_name_collision", "connect_failed"]

TRACE_TAG = "mcp.unavailable"


@dataclass(frozen=True)
class UnavailableServer:
    """One MCP server left out of the request, by display name."""

    name: str
    reason: UnavailableReason


@dataclass(frozen=True)
class McpBuild:
    """The servers a build or filter step kept, and the ones it could not."""

    servers: list[Any]
    unavailable: tuple[UnavailableServer, ...] = ()


def _names(unavailable: Sequence[UnavailableServer]) -> str:
    return ", ".join(sorted({server.name for server in unavailable}))


def model_note(unavailable: Sequence[UnavailableServer]) -> str:
    """System-prompt section naming the servers missing from this request; empty when none."""
    if not unavailable:
        return ""
    return (
        "## Unavailable tools\n"
        f"These configured tool servers are unavailable for this request: {_names(unavailable)}. "
        "If the user asks for something that needs them, say that this capability is currently "
        "unavailable. Do not substitute an unrelated tool or guess the result."
    )


def user_notice(unavailable: Sequence[UnavailableServer]) -> str:
    """Short notice shown to the user ahead of the answer; empty when none."""
    if not unavailable:
        return ""
    return f"Some tools are unavailable right now: {_names(unavailable)}. Answers that need them may be incomplete."


def trace_label(unavailable: Sequence[UnavailableServer]) -> str:
    """``name|reason`` pairs for the ``mcp.unavailable`` trace tag."""
    return ",".join(f"{server.name}|{server.reason}" for server in unavailable)
