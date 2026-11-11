"""Assemble the agent's instructions from the prompt body and per-request tails."""

from __future__ import annotations

import dataclasses
import datetime
from typing import TYPE_CHECKING, Any

from agents import Agent

from payloads.agent.runtime.prompt_cache import CONTEXT_SENTINEL, USER_TAIL_SENTINEL
from payloads.agent.runtime.request_context import get_request_username

if TYPE_CHECKING:
    from agents import RunContextWrapper

    from payloads.agent.runtime.plan import PlanState


def build_context_preamble(user: str = "unknown") -> str:
    """One-line context string with the current UTC time and username.

    Minute resolution is safe because the preamble goes into the context,
    which is sent after the history and never cached.  It is stamped once
    per request, so within a long turn it lags by the turn's duration.
    """
    now = datetime.datetime.now(datetime.UTC)
    return f"[Context: {now.strftime('%a %b %d %Y %H:%M')} (UTC) | User: {user}]"


def join_parts(*parts: str) -> str:
    """The non-empty *parts*, separated by blank lines."""
    return "\n\n".join(part for part in parts if part)


def assemble_instructions(body: str, user_tail: str, context: str) -> str:
    """*body*, then each non-empty tail after its sentinel (see :func:`payloads.agent.runtime.core.create_agent`)."""
    instructions = body
    if user_tail:
        instructions = f"{instructions}\n\n{USER_TAIL_SENTINEL}{user_tail}"
    if context:
        instructions = f"{instructions}\n\n{CONTEXT_SENTINEL}{context}"
    return instructions


@dataclasses.dataclass(frozen=True)
class PlanRecitingInstructions:
    """Instructions that recite the chat's plan at the end of the context before every model call."""

    body: str
    user_tail: str
    context: str
    plan: PlanState

    def __call__(self, _run_context: RunContextWrapper[Any], _agent: Agent[Any]) -> str:
        return assemble_instructions(self.body, self.user_tail, join_parts(self.context, self.plan.recite()))


def resolve_identity_preamble(config: dict[str, Any]) -> str:
    """Return the identity preamble for this request, or "" when disabled.

    Resolved once per request at the entry point rather than per LLM
    call, so every model call of one turn sees the same stamp.
    """
    if not config.get("INJECT_TIME_USER", True):
        return ""
    resolved_user = config.get("_resolved_username") or get_request_username() or "unknown"
    return build_context_preamble(resolved_user)
