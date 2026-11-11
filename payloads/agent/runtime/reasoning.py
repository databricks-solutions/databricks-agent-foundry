"""Reasoning ("thinking") content on Databricks chat completions.

When a Claude model thinks, the Databricks chat-completions endpoint
returns ``message.content`` as a *list* of blocks instead of a string::

    [{"type": "reasoning",
      "summary": [{"type": "summary_text", "text": "...", "signature": "..."}]},
     {"type": "text", "text": "..."}]

A summary entry may instead be ``{"type": "summary_encrypted_text",
"data": "..."}`` (redacted thinking).  Streaming deltas carry the same
block shapes in fragments.

The OpenAI Agents SDK's chat-completions converter only accepts a string
``content`` and crashes on the list.  It does, however, read reasoning
natively from a ``reasoning_content`` attribute on the message (or delta)
and turns it into a ``ResponseReasoningItem`` -- the same item the chat
UI renders as a collapsible reasoning section.  So the fix lives at the
client: :func:`normalize_completion` and :class:`NormalizedStream` fold
the text blocks into ``content`` and the summary text into
``reasoning_content`` before the SDK sees the response.  Both go through
one :class:`_ContentAccumulator`, so the two transports agree on what is
answer and what is reasoning.

``thinking_blocks`` -- the SDK's other reasoning attribute -- is left
unset on purpose: it copies signatures into the item's
``encrypted_content``, which would then leak through ``/invocations``.

What the SDK does *not* do on the chat-completions model is send the
signed reasoning back: ``items_to_messages`` drops reasoning items on
replay (only the LiteLLM model passes ``preserve_thinking_blocks``).
Databricks asks for the full, unmodified assistant message when the
model should reason over its earlier steps, and Anthropic expects the
signed blocks on the assistant message that made the tool calls.  So the
original content list is kept in a :class:`ReasoningReplayStore`, keyed
by tool-call id, and :func:`inject_reasoning` puts it back on the
outgoing assistant message.

Each agent run gets its own store (:func:`replay_scope`), so a store
cannot carry one conversation's reasoning into another's request.  The
chat UI's history carries no signatures, so earlier user turns would
arrive without their reasoning, and the prompt cache written with it
would stop matching at the first earlier assistant message (#681).  A
caller that sets ``custom_inputs.reasoning_replay`` therefore gets the
run's entries back in ``custom_outputs.reasoning_replay`` and sends them
with the next turn, which seeds that run's store: the caller holds the
state, so a request looks the same whichever worker serves it.
"""

from __future__ import annotations

import copy
import logging
from collections.abc import AsyncGenerator, Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from payloads.agent.runtime.content_filter import (
    CONTENT_FILTER_NOTICE,
    has_tool_calls,
    is_empty_content_filter,
)

logger = logging.getLogger(__name__)

# Block ``type`` values that carry reasoning rather than answer text.
# ``reasoning`` is the Databricks shape; ``thinking`` / ``redacted_thinking``
# are the Anthropic-native names, matched too so a block in either form is
# recognised (e.g. never given a ``cache_control`` marker).
REASONING_BLOCK_TYPES: Final = frozenset({"reasoning", "thinking", "redacted_thinking"})

# ``custom_inputs`` key a caller sets to ``false`` to receive no reasoning
# on ``/invocations``, overriding ``spec.runtime.reasoning.emit``.
INCLUDE_REASONING_INPUT: Final = "include_reasoning"

# ``custom_inputs`` key carrying earlier turns' replay entries (a list,
# possibly empty, which opts in), and the ``custom_outputs`` key the run's
# own entries come back under.
REASONING_REPLAY_KEY: Final = "reasoning_replay"

# Upper bound on seeded entries: one per model call that made tool calls.
MAX_REPLAY_ENTRIES: Final = 500

# Block types a seeded entry may contain: only what the model wrote, so a
# caller cannot add anything to an assistant message but its reasoning.
_REPLAYABLE_BLOCK_TYPES: Final = REASONING_BLOCK_TYPES | {"text"}

# Separator between the summaries of separate reasoning blocks.
# Display-only, and the same on both transports: the chat UI renders a
# single summary, and the replayed blocks never contain it.
_SUMMARY_SEPARATOR: Final = "\n\n"


# ---------------------------------------------------------------------------
# Content interpretation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SplitContent:
    """A list ``content`` separated into answer text and reasoning.

    ``text`` / ``summary`` are ``None`` rather than ``""`` when empty: the
    SDK opens a message item for any ``content`` that is not ``None``.
    ``replayable`` is true when a reasoning block carries a signature or
    encrypted data -- the blocks worth sending back.
    """

    text: str | None
    summary: str | None
    replayable: bool


def _block_type(block: Any) -> str | None:
    if not isinstance(block, Mapping):
        return None
    value = block.get("type")
    return value if isinstance(value, str) else None


def _summary_entries(block: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    summary = block.get("summary")
    if not isinstance(summary, list):
        return []
    return [entry for entry in summary if isinstance(entry, Mapping)]


def _is_signed(block: Mapping[str, Any]) -> bool:
    """Whether *block* (or a summary entry) carries anything the provider validates."""
    return bool(block.get("signature") or block.get("data"))


def _is_blank_text(block: Any) -> bool:
    return _block_type(block) == "text" and not str(block.get("text") or "").strip()


def _without_blank_text(content: Iterable[Any]) -> list[Any]:
    """*content* minus whitespace-only text blocks.

    Anthropic rejects them on the way in, so they cannot be replayed; the
    signatures cover the reasoning blocks only, so dropping them changes
    nothing the provider verifies.
    """
    return [block for block in content if not _is_blank_text(block)]


class _ContentAccumulator:
    """Interprets list ``content``, delivered whole or in streamed fragments.

    The one place that decides what is answer text and what is reasoning,
    for both transports, while rebuilding the original blocks for replay.
    Fragments of the same kind merge into the open block; a signature (or
    encrypted data), or a switch between text and reasoning, closes it.
    No separator is ever written into the rebuilt blocks -- the signature
    covers their exact text -- only into the *display* summary.
    """

    def __init__(self) -> None:
        self._blocks: list[dict[str, Any]] = []
        self._open: str | None = None
        self._summary_shown = False
        self._separator_due = False
        self.replayable = False

    def feed(self, content: Iterable[Any], *, whole_blocks: bool = False) -> tuple[str, str]:
        """Add *content*; return the answer text and summary text it carries.

        *whole_blocks* closes every reasoning block after it is added, for
        a non-streamed response whose blocks are complete as delivered.
        """
        texts: list[str] = []
        summaries: list[str] = []
        for block in content:
            kind = _block_type(block)
            if kind == "text":
                texts.append(self.add_text(str(block.get("text") or "")))
            elif kind in REASONING_BLOCK_TYPES:
                summaries.append(self._add_reasoning(block))
                if whole_blocks:
                    self._open = None
            elif isinstance(block, Mapping):
                # Nothing to show, but part of the message: kept for replay.
                self._blocks.append(copy.deepcopy(dict(block)))
                self._open = None
        return "".join(texts), "".join(summaries)

    def add_text(self, fragment: str) -> str:
        if not fragment:
            return ""
        if self._open == "text":
            self._blocks[-1]["text"] += fragment
        else:
            self._blocks.append({"type": "text", "text": fragment})
            self._open = "text"
        return fragment

    def replay_content(self) -> list[Any]:
        """The rebuilt message content, ready to be sent back."""
        return _without_blank_text(self._blocks)

    def _add_reasoning(self, block: Mapping[str, Any]) -> str:
        if _block_type(block) != "reasoning":
            # Anthropic-native thinking blocks have no ``summary``; keep
            # them as delivered.
            self._blocks.append(copy.deepcopy(dict(block)))
            self._open = None
            self.replayable = self.replayable or _is_signed(block)
            return ""
        extras = {key: copy.deepcopy(value) for key, value in block.items() if key not in {"type", "summary"}}
        extras = {key: value for key, value in extras.items() if value not in (None, "")}
        entries = _summary_entries(block)
        if not entries and not extras:
            # An empty fragment carries nothing; it must not close (or
            # open) a block, or the replay gains a block the model never wrote.
            return ""
        shown: list[str] = []
        target: dict[str, Any] | None = None
        signed = False
        for entry in entries:
            if entry.get("type") != "summary_text":
                # Encrypted (or unknown) summaries are opaque: kept whole,
                # as a block of their own.
                self._open = None
                target = {"type": "reasoning", "summary": [copy.deepcopy(dict(entry))]}
                self._blocks.append(target)
                signed = signed or _is_signed(entry)
                continue
            target = self._open_reasoning()
            current = target["summary"][0]
            text = str(entry.get("text") or "")
            current["text"] += text
            for key, value in entry.items():
                if key not in {"type", "text"} and value not in (None, ""):
                    current[key] = copy.deepcopy(value)
            signed = signed or _is_signed(entry)
            if text:
                shown.append(self._shown(text))
        if extras:
            # Block-level fields (``signature``, ``id``, ...) may arrive on
            # any fragment, not only the one that opened the block.
            if target is None:
                target = self._open_reasoning()
            target.update(extras)
            signed = signed or _is_signed(extras)
        if signed:
            self.replayable = True
            self._open = None
        return "".join(shown)

    def _open_reasoning(self) -> dict[str, Any]:
        if self._open != "reasoning":
            self._blocks.append({"type": "reasoning", "summary": [{"type": "summary_text", "text": ""}]})
            self._open = "reasoning"
            self._separator_due = self._summary_shown
        return self._blocks[-1]

    def _shown(self, text: str) -> str:
        prefix = _SUMMARY_SEPARATOR if self._separator_due else ""
        self._separator_due = False
        self._summary_shown = True
        return prefix + text


def split_content(content: Iterable[Any]) -> SplitContent:
    """Separate a list ``content`` into answer text and reasoning summary.

    Text blocks are concatenated with no separator, exactly as a stream
    would have delivered them; summaries of separate reasoning blocks are
    separated by a blank line, on both transports.  Unknown block types
    are ignored here -- they survive in the stored original for replay.
    """
    accumulator = _ContentAccumulator()
    text, summary = accumulator.feed(content, whole_blocks=True)
    return SplitContent(text=text or None, summary=summary or None, replayable=accumulator.replayable)


def has_reasoning_block(content: Any) -> bool:
    """Whether a message ``content`` already carries a reasoning block."""
    return isinstance(content, list) and any(_block_type(block) in REASONING_BLOCK_TYPES for block in content)


def _plain_text(content: Any) -> str:
    """The answer text of an outgoing message ``content``, stripped.

    Stripping makes ``None``, ``""`` and the ``" "`` placeholder that
    ``databricks-openai`` writes into empty tool-call messages compare equal.
    """
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "".join(str(block.get("text") or "") for block in content if _block_type(block) == "text").strip()
    return ""


# ---------------------------------------------------------------------------
# Replay store
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _StoredReasoning:
    model: str
    content: list[Any]
    text: str


class ReplayEntry(BaseModel):
    """One model call's original content, as exchanged with the caller.

    Empty ``content`` -- a call without signed reasoning -- only records
    which tool calls were made together (see :func:`regroup_parallel_calls`).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str = Field(min_length=1)
    tool_call_ids: list[str] = Field(min_length=1)
    content: list[dict[str, Any]]


def _is_seedable(entry: ReplayEntry) -> bool:
    """Whether *entry* groups parallel calls, or holds only reasoning and text with signed reasoning."""
    if not entry.content:
        return len(entry.tool_call_ids) > 1
    if any(_block_type(block) not in _REPLAYABLE_BLOCK_TYPES for block in entry.content):
        return False
    return split_content(entry.content).replayable


class ReasoningReplayStore:
    """Original assistant content with signed reasoning, by tool-call id.

    One store serves one agent run (see :func:`replay_scope`) and is
    dropped with it, so it holds only that run's own responses and the
    entries it was seeded with, and needs no expiry.  Parallel tool calls
    share one entry.
    """

    def __init__(self) -> None:
        self._entries: dict[str, _StoredReasoning] = {}
        self._produced: list[ReplayEntry] = []

    def put(self, model: str, tool_call_ids: Iterable[str], content: list[Any]) -> None:
        """Remember *content* as the assistant message behind *tool_call_ids*.

        Empty *content* records only that the calls were made together, so
        it is kept for two or more calls and never replayed.
        """
        ids = [call_id for call_id in tool_call_ids if call_id]
        if not ids or (not content and len(ids) < 2):
            return
        if content:
            self._index(model, ids, content)
        self._produced.append(ReplayEntry(model=model, tool_call_ids=ids, content=copy.deepcopy(content)))

    def seed(self, entries: Iterable[ReplayEntry]) -> None:
        """Add earlier turns' *entries*; they are not exported again."""
        for entry in entries:
            if entry.content:
                self._index(entry.model, entry.tool_call_ids, entry.content)

    def produced(self) -> list[ReplayEntry]:
        """The entries this run's own model calls stored, in call order."""
        return list(self._produced)

    def _index(self, model: str, ids: list[str], content: list[Any]) -> None:
        entry = _StoredReasoning(model=model, content=copy.deepcopy(content), text=_plain_text(content))
        for call_id in ids:
            self._entries[call_id] = entry

    def get(self, model: str, tool_call_id: str) -> _StoredReasoning | None:
        """Return the entry for *tool_call_id* if it came from *model*."""
        entry = self._entries.get(tool_call_id)
        return entry if entry is not None and entry.model == model else None

    def __len__(self) -> int:
        return len(self._entries)


# The store of the agent run in progress.  A ContextVar rather than a
# module global so concurrent runs never share one; ``None`` outside any
# run, where nothing is stored or replayed.
_RUN_STORE: Final[ContextVar[ReasoningReplayStore | None]] = ContextVar("reasoning_replay_store", default=None)


@contextmanager
def replay_scope(seed: Iterable[ReplayEntry] = ()) -> Iterator[ReasoningReplayStore]:
    """Give the agent run started inside this block a replay store of its own.

    The store starts with *seed*, the entries of earlier turns.  Wrap the
    ``await Runner.run(...)``, or just the ``Runner.run_streamed(...)``
    call: ``run_streamed`` starts the run as a task, which copies the context
    when it is created, so the store stays with the run after the block exits.
    ``test_streamed_tool_loop_reaches_invocations_events`` fails if the SDK
    ever starts that task later.
    """
    store = ReasoningReplayStore()
    store.seed(seed)
    token = _RUN_STORE.set(store)
    try:
        yield store
    finally:
        _RUN_STORE.reset(token)


def current_replay_store() -> ReasoningReplayStore | None:
    """Return the store of the run in progress, or ``None`` outside a run."""
    return _RUN_STORE.get()


def _tool_call_id(tool_call: Any) -> str | None:
    value = tool_call.get("id") if isinstance(tool_call, Mapping) else getattr(tool_call, "id", None)
    return value if isinstance(value, str) and value else None


def inject_reasoning(
    messages: list[Any] | None,
    model: str | None,
    store: ReasoningReplayStore | None = None,
) -> int:
    """Put stored reasoning back on outgoing assistant tool-call messages.

    An assistant message is rewritten only when the store holds an entry
    for its first tool call from the same *model*, it carries no reasoning
    block yet, and its text still matches the stored original -- anything
    else means the message is not the one the reasoning was signed for.
    Mutates *messages* in place and returns the number rewritten;
    idempotent, because a retried ``create()`` re-sends the same list.
    """
    resolved = store if store is not None else _RUN_STORE.get()
    if resolved is None or not messages or not model:
        return 0
    injected = 0
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        tool_calls = message.get("tool_calls")
        if not isinstance(tool_calls, list) or not tool_calls:
            continue
        content = message.get("content")
        if has_reasoning_block(content):
            continue
        call_id = _tool_call_id(tool_calls[0])
        entry = resolved.get(model, call_id) if call_id else None
        if entry is None:
            continue
        if _plain_text(content) != entry.text:
            logger.debug("Skipping reasoning replay for %s: assistant text changed", call_id)
            continue
        # One level deep is enough: the only later mutation is the cache
        # marker that ``apply_cache_breakpoints`` sets on a block itself.
        message["content"] = [dict(block) if isinstance(block, dict) else block for block in entry.content]
        injected += 1
    return injected


# ---------------------------------------------------------------------------
# Non-streamed responses
# ---------------------------------------------------------------------------


def normalize_completion(
    completion: Any,
    *,
    model: str | None,
    store: ReasoningReplayStore | None = None,
) -> None:
    """Rewrite list ``content`` on a ``ChatCompletion`` in place.

    In place, not as a copy, because the SDK only accepts the response if
    it is still a ``ChatCompletion`` instance.  A string ``content`` is
    left untouched, so non-thinking models see no change at all.
    """
    resolved = store if store is not None else _RUN_STORE.get()
    for choice in getattr(completion, "choices", None) or []:
        message = getattr(choice, "message", None)
        if message is None:
            continue
        content = getattr(message, "content", None)
        finish_reason = getattr(choice, "finish_reason", None)
        call_ids = [call_id for call in (message.tool_calls or []) if (call_id := _tool_call_id(call))]
        replay: list[Any] = []
        if isinstance(content, list):
            split = split_content(content)
            message.content = split.text
            if split.summary:
                message.reasoning_content = split.summary
            if split.replayable:
                replay = _without_blank_text(content)
            if split.text is None and not call_ids and finish_reason == "length":
                logger.warning("Model spent its output budget on reasoning; the answer is empty (finish_reason=length)")
        if resolved is not None and call_ids and model:
            resolved.put(model, call_ids, replay)
        if is_empty_content_filter(
            finish_reason,
            text=message.content if isinstance(message.content, str) else None,
            has_tool_calls=has_tool_calls(message),
        ):
            message.content = CONTENT_FILTER_NOTICE
            logger.warning("Provider refused the turn (finish_reason=%s); substituting notice", finish_reason)


# ---------------------------------------------------------------------------
# Streamed responses
# ---------------------------------------------------------------------------


def _normalize_delta(delta: Any, accumulator: _ContentAccumulator) -> None:
    content = getattr(delta, "content", None)
    if isinstance(content, str):
        accumulator.add_text(content)
        return
    if not isinstance(content, list):
        return
    text, summary = accumulator.feed(content)
    delta.content = text or None
    if summary:
        delta.reasoning_content = summary


class NormalizedStream:
    """A chat-completions stream whose list ``content`` deltas are rewritten.

    Wraps the upstream ``AsyncStream`` (already wrapped by MLflow autolog,
    which therefore traces the raw chunks) and supports the same ways of
    consuming it: ``async for``, ``anext()``, ``async with`` and
    ``close()``; anything else -- ``response``, ... -- is delegated.  When
    the stream completes, reasoning behind tool calls is stored for replay
    exactly as :func:`normalize_completion` does.
    """

    def __init__(
        self,
        upstream: Any,
        *,
        model: str | None,
        store: ReasoningReplayStore | None = None,
    ) -> None:
        self._upstream = upstream
        self._model = model
        # Resolved now, inside ``create()``: that is the run's context.
        self._store = store if store is not None else _RUN_STORE.get()
        self._iterator = self._iterate()

    def __aiter__(self) -> NormalizedStream:
        return self

    async def __anext__(self) -> Any:
        return await self._iterator.__anext__()

    async def __aenter__(self) -> NormalizedStream:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def _iterate(self) -> AsyncGenerator[Any, None]:
        accumulator = _ContentAccumulator()
        call_ids: list[str] = []
        saw_text = False
        try:
            async for chunk in self._upstream:
                for choice in getattr(chunk, "choices", None) or []:
                    if getattr(choice, "index", 0) != 0:
                        continue
                    delta = getattr(choice, "delta", None)
                    if delta is None:
                        continue
                    before = getattr(delta, "content", None)
                    _normalize_delta(delta, accumulator)
                    after = getattr(delta, "content", None)
                    if isinstance(after, str) and after.strip():
                        saw_text = True
                    elif isinstance(before, str) and before.strip():
                        saw_text = True
                    for call in getattr(delta, "tool_calls", None) or []:
                        call_id = _tool_call_id(call)
                        if call_id and call_id not in call_ids:
                            call_ids.append(call_id)
                    if is_empty_content_filter(
                        getattr(choice, "finish_reason", None),
                        text="" if not saw_text else "x",
                        has_tool_calls=bool(call_ids),
                    ):
                        delta.content = CONTENT_FILTER_NOTICE
                        logger.warning(
                            "Provider refused the turn (finish_reason=%s); substituting notice",
                            choice.finish_reason,
                        )
                yield chunk
        finally:
            await self._close_upstream()
        if self._store is not None and call_ids and self._model:
            replay = accumulator.replay_content() if accumulator.replayable else []
            self._store.put(self._model, call_ids, replay)

    async def close(self) -> None:
        """Stop the stream and release the upstream connection."""
        await self._iterator.aclose()
        # A stream closed before its first chunk never ran the finally.
        await self._close_upstream()

    aclose = close

    async def _close_upstream(self) -> None:
        close = getattr(self._upstream, "close", None)
        if close is None:
            return
        result = close()
        if hasattr(result, "__await__"):
            await result

    def __getattr__(self, name: str) -> Any:
        # Private names are never delegated: during construction (or a
        # copy) ``_upstream`` itself may be missing, and delegating it
        # would recurse.
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._upstream, name)


# ---------------------------------------------------------------------------
# Emission
# ---------------------------------------------------------------------------


def resolve_emit_reasoning(
    config: Mapping[str, Any] | None,
    custom_inputs: Mapping[str, Any] | None,
) -> bool:
    """Whether this request's response should carry reasoning.

    ``custom_inputs.include_reasoning`` (a bool, or the string ``"true"`` /
    ``"false"``) wins; otherwise the deployment default from
    ``spec.runtime.reasoning.emit`` applies.
    """
    override = (custom_inputs or {}).get(INCLUDE_REASONING_INPUT)
    if isinstance(override, str):
        # Some clients send every custom input as a string.
        override = {"true": True, "false": False}.get(override.strip().lower(), override)
    if isinstance(override, bool):
        return override
    if override is not None:
        logger.warning("Ignoring custom_inputs.%s=%r: expected true or false", INCLUDE_REASONING_INPUT, override)
    default = (config or {}).get("REASONING_EMIT")
    return True if default is None else bool(default)


def parse_replay_entries(custom_inputs: Mapping[str, Any] | None) -> list[ReplayEntry] | None:
    """The earlier turns' entries from *custom_inputs*; ``None`` when not opted in.

    Invalid entries are skipped with a warning rather than failing the
    request: a missing entry only costs a cache miss.
    """
    raw = (custom_inputs or {}).get(REASONING_REPLAY_KEY)
    if raw is None:
        return None
    if not isinstance(raw, list):
        logger.warning("Ignoring custom_inputs.%s: expected a list", REASONING_REPLAY_KEY)
        return []
    if len(raw) > MAX_REPLAY_ENTRIES:
        logger.warning(
            "custom_inputs.%s has %d entries; keeping the last %d", REASONING_REPLAY_KEY, len(raw), MAX_REPLAY_ENTRIES
        )
    entries: list[ReplayEntry] = []
    for item in raw[-MAX_REPLAY_ENTRIES:]:
        try:
            entry = ReplayEntry.model_validate(item)
        except ValidationError:
            logger.warning("Skipping an invalid custom_inputs.%s entry", REASONING_REPLAY_KEY)
            continue
        if _is_seedable(entry):
            entries.append(entry)
        else:
            logger.warning("Skipping a custom_inputs.%s entry without signed reasoning only", REASONING_REPLAY_KEY)
    return entries


def _call_key(item: Any) -> tuple[Any, Any]:
    return (item.get("type"), item.get("call_id")) if isinstance(item, Mapping) else (None, None)


def _regroup(items: list[Any], call_ids: list[str]) -> list[Any]:
    """*items* with the alternating calls and outputs of *call_ids* as all calls, then all outputs."""
    expected = [(kind, call_id) for call_id in call_ids for kind in ("function_call", "function_call_output")]
    for start in range(len(items) - len(expected) + 1):
        span = items[start : start + len(expected)]
        if [_call_key(item) for item in span] == expected:
            return [*items[:start], *span[0::2], *span[1::2], *items[start + len(expected) :]]
    return items


def regroup_parallel_calls(items: list[Any], entries: Iterable[ReplayEntry]) -> list[Any]:
    """Restore the shape a model call with parallel tool calls had in its own run.

    The chat history stores each tool call with its output, so a call that
    made several arrives as alternating calls and outputs, which the SDK
    turns into one assistant message per call: neither the prompt cache
    nor the replay entry matches that.  An entry names the calls one model
    call made, so only those, when found next to each other in that order,
    are put back as all calls, then all outputs.
    """
    for entry in entries:
        if len(entry.tool_call_ids) > 1:
            items = _regroup(items, entry.tool_call_ids)
    return items


def replay_outputs(store: ReasoningReplayStore, *, export: bool) -> dict[str, Any] | None:
    """The ``custom_outputs`` entry with this run's replay entries, or ``None``."""
    entries = store.produced() if export else []
    if not entries:
        return None
    return {REASONING_REPLAY_KEY: [entry.model_dump() for entry in entries]}


__all__ = [
    "INCLUDE_REASONING_INPUT",
    "MAX_REPLAY_ENTRIES",
    "REASONING_BLOCK_TYPES",
    "REASONING_REPLAY_KEY",
    "NormalizedStream",
    "ReasoningReplayStore",
    "ReplayEntry",
    "SplitContent",
    "current_replay_store",
    "has_reasoning_block",
    "inject_reasoning",
    "normalize_completion",
    "parse_replay_entries",
    "regroup_parallel_calls",
    "replay_outputs",
    "replay_scope",
    "resolve_emit_reasoning",
    "split_content",
]
