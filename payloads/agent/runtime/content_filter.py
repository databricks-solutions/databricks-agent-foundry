"""Fail-soft handling when the provider refuses a turn as ``content_filter``.

Databricks FMAPI chat completions map Anthropic ``stop_reason: refusal``
(and Bedrock ``content_filtered``) to OpenAI-compatible
``finish_reason: "content_filter"``.  The HTTP status is still 200, the
assistant ``content`` is empty, and ``completion_tokens`` is 0.  Left as
a successful empty message, the chat UI looks like the agent ignored the
user (issue #579).

This is not an exception path: unlike a context-window 400, the Agents
SDK never raises.  The completions normaliser therefore substitutes a
short notice when the turn has no text and no tool calls.  The chat UI
server recognises the notice and leaves the refused turn out of later
model requests (``lib/refused-turns.ts``), so one refusal does not make
the rest of the chat fail.
"""

from __future__ import annotations

from typing import Any, Final

# Databricks FMAPI uses the OpenAI name; Bedrock Converse may pass through
# the native token.  Match both so a mapping change does not restore silence.
CONTENT_FILTER_FINISH_REASONS: Final = frozenset({"content_filter", "content_filtered"})

# Shown in place of the empty assistant turn.  Plain wording: words about
# hidden reasoning or rules can themselves look like a jailbreak to the
# provider's filter.  Must match ``CONTENT_FILTER_NOTICE`` in the chat UI's
# ``lib/refused-turns.ts``.
CONTENT_FILTER_NOTICE: Final = (
    "I couldn't answer that: the model provider declined the request. Please rephrase it and try again."
)


def is_empty_content_filter(
    finish_reason: str | None,
    *,
    text: str | None,
    has_tool_calls: bool,
) -> bool:
    """Whether this choice is a refused turn with nothing to show."""
    if finish_reason not in CONTENT_FILTER_FINISH_REASONS:
        return False
    if has_tool_calls:
        return False
    return not (text or "").strip()


def has_tool_calls(message: Any) -> bool:
    """Whether *message* already carries tool calls the SDK must keep."""
    calls = getattr(message, "tool_calls", None) if message is not None else None
    return bool(calls)


__all__ = [
    "CONTENT_FILTER_FINISH_REASONS",
    "CONTENT_FILTER_NOTICE",
    "has_tool_calls",
    "is_empty_content_filter",
]
