"""Fail-soft handling for failures the user can act on.

Two provider rejections get a notice instead of an error: the context window
(below) and an image sent to a model without vision.  The image stays in the
history, so every later turn of that conversation fails the same way --
the notice says to start a new one.  A run that reaches ``max_iterations``
gets one too, asking the user to reply "continue".

When a conversation outgrows the model's window the gateway returns a 400
-- Anthropic's is ``prompt is too long: N tokens > 200000 maximum`` -- and
the framework has no way to make the next turn smaller: history arrives
complete on ``request.input`` and every retry replays it.  Left uncaught,
the exception escapes ``Runner.run`` and reaches the user as a raw
provider string, and the chat UI's streaming fallback
(``fallbackToGenerateText``) re-sends the identical oversized payload, so
one unanswerable turn costs two rejected requests.

Catching it here does not make the turn succeed.  It converts an opaque
failure into an explanation the user can act on, and -- because the
handlers return a normal response instead of raising -- stops the UI from
retrying something that cannot work.

Detection is by message substring because the OpenAI-compatible surface
gives no structured code for this: the Databricks gateway and the SDK each
re-wrap the provider error.  Callers must therefore treat a
non-match as "not this failure" and re-raise, so an unrecognised error is
never silently swallowed.
"""

from __future__ import annotations

from typing import Final

from agents.exceptions import MaxTurnsExceeded

# Substrings that identify a context-window rejection, lower-cased.
# Anthropic's phrasing arrives via the Databricks FMAPI passthrough; the
# rest cover the OpenAI-compatible endpoints served through the same
# gateway.  Matching several vendors is deliberate -- the runtime targets
# whatever model the payload's ``spec.runtime.model`` points at.
CONTEXT_LIMIT_MARKERS: Final = (
    "prompt is too long",
    "context_length_exceeded",
    "maximum context length",
    "reduce the length of the messages",
)

# Shown to the user in place of the failed turn.  Deliberately says what
# to do next and that nothing was lost from the transcript, because the
# alternative reading -- "the agent is broken" -- is what generates
# support tickets.
CONTEXT_LIMIT_NOTICE: Final = (
    "This conversation has grown longer than the model's context window, so "
    "I could not answer that turn. Please start a new conversation to "
    "continue. Nothing here is lost -- the transcript is still saved, it "
    "just can no longer be sent to the model in full."
)

# The chat UI's variant while conversation compaction is on: the turn's
# ``limit_exceeded`` report makes it summarize the older messages in the
# background, after which the conversation fits again.
COMPACTING_CONTEXT_LIMIT_NOTICE: Final = (
    "This conversation has grown longer than the model's context window, so "
    "I could not answer that turn. I am summarizing the earlier messages now; "
    "please send your message again in a moment. Nothing here is lost -- the "
    "transcript is still saved."
)

# Databricks serving and the Unity Gateway answer an image sent to a
# text-only model with ``Image input is not supported for this endpoint``.
IMAGE_INPUT_MARKERS: Final = ("image input is not supported",)

IMAGE_INPUT_NOTICE: Final = (
    "The model behind this agent can't read images, so I could not answer "
    "that turn. Please start a new conversation without the image to "
    "continue -- the image stays in this conversation and would be sent "
    "again with every message."
)

MAX_TURNS_NOTICE: Final = (
    "I reached the limit of steps I can take in one reply before finishing. "
    'Reply "continue" and I will pick up where I stopped.'
)

# Depth bound on the ``__cause__`` / ``__context__` walk.  The SDK and the
# HTTP client both re-raise, so the marker is usually one or two levels
# down; the bound stops a pathological chain (or a cycle) from hanging the
# error path, which is the worst possible place to hang.
_MAX_CAUSE_DEPTH: Final = 5


def _chain_matches(exc: BaseException, markers: tuple[str, ...]) -> bool:
    """Whether any exception in *exc*'s chain contains one of *markers*.

    Walks the chain, since the marker is normally raised by the HTTP layer
    and re-wrapped before it reaches the agent loop.
    """
    current: BaseException | None = exc
    for _ in range(_MAX_CAUSE_DEPTH):
        if current is None:
            break
        text = str(current).lower()
        if any(marker in text for marker in markers):
            return True
        current = current.__cause__ or current.__context__
    return False


def is_context_limit_error(exc: BaseException) -> bool:
    """Whether *exc* is the provider refusing an oversized prompt."""
    return _chain_matches(exc, CONTEXT_LIMIT_MARKERS)


def fail_soft_notice(exc: BaseException, *, compaction: bool = False) -> str | None:
    """The user notice for *exc*, or ``None`` when it must be re-raised.

    *compaction* selects the context-limit wording for a chat whose older
    messages are about to be summarized.
    """
    if is_context_limit_error(exc):
        return COMPACTING_CONTEXT_LIMIT_NOTICE if compaction else CONTEXT_LIMIT_NOTICE
    if _chain_matches(exc, IMAGE_INPUT_MARKERS):
        return IMAGE_INPUT_NOTICE
    if isinstance(exc, MaxTurnsExceeded):
        return MAX_TURNS_NOTICE
    return None


__all__ = [
    "COMPACTING_CONTEXT_LIMIT_NOTICE",
    "CONTEXT_LIMIT_MARKERS",
    "CONTEXT_LIMIT_NOTICE",
    "IMAGE_INPUT_MARKERS",
    "IMAGE_INPUT_NOTICE",
    "MAX_TURNS_NOTICE",
    "fail_soft_notice",
    "is_context_limit_error",
]
