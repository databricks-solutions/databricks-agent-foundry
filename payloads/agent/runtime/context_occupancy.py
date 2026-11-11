"""How full the model's context window was on the last call of a request.

The chat UI server decides when to compact a conversation (#637), but it
cannot measure occupancy itself: it never sees the system prompt, the
skill bodies or the tool schemas.  The runtime does, so it reports the
number and the UI acts on it.  The number is the size before
:mod:`~payloads.agent.runtime.tool_result_clearing` runs: clearing lasts
one run, so the next turn starts from the uncleared history, and a
post-clearing number could keep slipping under the compaction trigger
while every turn clears again (#672).

When the provider reports the last call's input tokens, the number is that
count scaled back to the uncleared size by the ratio of the two character
estimates, so only the cleared share is estimated; otherwise it is the
character estimate, which counts high.

The value travels back in ``custom_outputs`` under
:data:`CONTEXT_WINDOW_KEY`, next to whatever an instance ``finalize`` hook
returns.  A dedicated key rather than a generic ``context`` keeps it from
colliding with hook output.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

CONTEXT_WINDOW_KEY: Final = "context_window"


@dataclass
class OccupancyRecorder:
    """Estimated prompt tokens of the most recent model call in one request.

    One recorder per request, captured by that request's model-input filter.
    A shared mutable object rather than a ``ContextVar``: a streamed run
    executes in a task holding a copy of the caller's context, so a value
    set inside the run would never reach the generator that reports it.
    """

    estimated_input_tokens: int | None = None
    # The same call's estimate as sent, after clearing.
    sent_estimated_input_tokens: int | None = None

    def record(self, estimated_input_tokens: int) -> None:
        self.estimated_input_tokens = estimated_input_tokens
        self.sent_estimated_input_tokens = estimated_input_tokens

    def record_sent(self, sent_estimated_input_tokens: int) -> None:
        self.sent_estimated_input_tokens = sent_estimated_input_tokens

    def input_tokens(self, reported_input_tokens: int | None) -> int | None:
        """The last call's size before clearing, from the provider's count when there is one."""
        estimate, sent = self.estimated_input_tokens, self.sent_estimated_input_tokens
        if estimate is None or not reported_input_tokens or not sent:
            return estimate
        return round(reported_input_tokens * estimate / sent)


def reported_input_tokens(raw_responses: Sequence[Any]) -> int | None:
    """The provider's input-token count of the last model response, or ``None`` without one."""
    if not raw_responses:
        return None
    tokens = getattr(getattr(raw_responses[-1], "usage", None), "input_tokens", None)
    return tokens if isinstance(tokens, int) and tokens > 0 else None


def context_window_outputs(
    recorder: OccupancyRecorder | None,
    *,
    reported_input_tokens: int | None = None,
    limit_exceeded: bool = False,
) -> dict[str, Any] | None:
    """The ``custom_outputs`` entry for this request, or ``None`` when empty.

    *reported_input_tokens* is the provider's count for the call the
    recorder saw last; leave it out when that call did not complete.
    """
    entry: dict[str, Any] = {}
    input_tokens = recorder.input_tokens(reported_input_tokens) if recorder is not None else None
    if input_tokens is not None:
        entry["estimated_input_tokens"] = input_tokens
    if limit_exceeded:
        entry["limit_exceeded"] = True
    return {CONTEXT_WINDOW_KEY: entry} if entry else None


def merge_custom_outputs(
    hook_outputs: dict[str, Any] | None,
    *framework_outputs: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Combine the ``finalize`` hook's dict with the framework's entries.

    A framework entry wins on a key clash, because the UI server relies
    on its shape.  ``None`` when all are empty, preserving the rule that
    the stream path emits no trailing event without a payload.
    """
    merged = dict(hook_outputs or {})
    for outputs in framework_outputs:
        merged.update(outputs or {})
    return merged or None


__all__ = [
    "CONTEXT_WINDOW_KEY",
    "OccupancyRecorder",
    "context_window_outputs",
    "merge_custom_outputs",
    "reported_input_tokens",
]
