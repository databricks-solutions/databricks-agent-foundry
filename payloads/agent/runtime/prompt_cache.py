"""Provider prompt caching for the agent's outgoing chat completions.

Databricks' Claude endpoints bill an input token at 0.1x when it is read
from the provider's prompt cache instead of sent fresh.  Getting that
discount needs a ``cache_control`` marker on specific content *blocks*
of the request, which the OpenAI Agents SDK gives us no seam for: it
serialises ``Agent.instructions`` into a bare ``{"role": "system",
"content": <str>}``, and a string cannot carry a marker.

So the injection point is the client.  :class:`PromptCachedCompletions`
wraps ``chat.completions.create``, rewrites the message list on its way
out, and calls ``super().create()`` last so the library's own payload
fixes and MLflow's tracing patch stay in the chain.

Caching is **cumulative from the start of the prompt**: everything up to
and including a marked block is cached together.  Two consequences drive
the whole design.  Anything volatile early in the prompt invalidates
every breakpoint after it, not just its own -- which is why
:func:`split_user_tail_message` moves the per-user tail out of the system
message into one of its own, behind the breakpoint, and
:func:`move_context_to_end` puts the per-request context (user, time,
memory index, plan) after the whole history, before any marker is
placed.  And the marked prefix must be byte-identical across requests to
be reused at all.

The TTL is a sliding window refreshed free on every hit, so a breakpoint
stays warm as long as traffic keeps touching it; a full-price write is
paid only when the prefix is cold or its bytes changed.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from databricks_openai import AsyncDatabricksOpenAI
from databricks_openai.utils.clients import AsyncDatabricksCompletions
from openai import AsyncStream
from openai.types.chat import ChatCompletion

from payloads.agent.runtime.reasoning import (
    REASONING_BLOCK_TYPES,
    NormalizedStream,
    inject_reasoning,
    normalize_completion,
)

logger = logging.getLogger(__name__)

# Anthropic's hard limit on ``cache_control`` markers per request.  A
# provider constraint, not a preference -- hence a constant rather than
# an authored knob.  The placement policy below spends 3 and leaves one
# spare.
MAX_BREAKPOINTS: Final = 4

# Separator ``create_agent`` writes between the cacheable body of the
# system prompt and the per-user tail (skill index, model note).  The prompt
# stays a single string all the way through assembly -- one path, one
# place that decides what the model reads -- and the split into two
# messages happens here, at the only layer that can add a message without
# it being persisted into conversation history.  Never reaches the
# provider: :func:`split_user_tail_message` consumes it on every request,
# cached or not.
USER_TAIL_SENTINEL: Final = "<!-- af:user-tail -->"

# Separator ``create_agent`` writes before the per-request context, after
# the user tail.  Consumed by :func:`move_context_to_end`, which
# sends the text as the last message: it changes between turns, and
# anywhere before the history it would invalidate every cached history
# token behind it.
CONTEXT_SENTINEL: Final = "<!-- af:context -->"

# Role carried by the user-tail message.  ``system`` rather than ``user``
# because the text is framework-supplied context, not something the user
# said, and rather than ``developer`` because that role costs two extra
# tokens per request on the gateway's mapping for no behavioural gain.
USER_TAIL_ROLE: Final = "system"

# Role of the trailing context message, ``system`` for the same reason.
# Being system-role is also what keeps a rolling breakpoint off it.
CONTEXT_ROLE: Final = "system"

# Opening line of the trailing context message.  Without it the model,
# seeing system text right after tool results, stops to judge whether a
# tool injected it.
CONTEXT_HEADER: Final = (
    "Runtime context from the agent framework, refreshed before every model call. "
    "It is not part of any tool result or user message."
)

# Substring that marks a model as supporting per-block ``cache_control``.
# Mirrors ``databricks_openai.utils.clients._is_claude_model``; owned
# here rather than imported because production code should not depend on
# a private symbol.  ``tests/test_prompt_cache.py`` asserts the two stay
# in agreement, so an upstream change fails loudly.
CACHEABLE_MODEL_MARKERS: Final = ("claude",)


@dataclass(frozen=True)
class PromptCacheSettings:
    """Resolved ``spec.runtime.context.prompt_cache`` values.

    ``rolling_ttl=None`` means "omit the ``ttl`` key", which selects the
    endpoint's own 5-minute default and keeps the payload minimal.
    """

    enabled: bool = True
    static_ttl: str | None = "1h"
    rolling_ttl: str | None = None

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> PromptCacheSettings:
        """Build settings from the runtime config dict.

        Missing keys fall back to the field defaults, which mirror
        :class:`payloads.agent.config.AgentConfig` so unit tests and
        production agree.
        """
        defaults = cls()
        enabled = config.get("PROMPT_CACHE_ENABLED")
        return cls(
            enabled=defaults.enabled if enabled is None else bool(enabled),
            static_ttl=config.get("PROMPT_CACHE_STATIC_TTL", defaults.static_ttl),
            rolling_ttl=config.get("PROMPT_CACHE_ROLLING_TTL", defaults.rolling_ttl),
        )


# Process-level settings, pushed in by ``configure_prompt_cache`` once
# config is loaded.  Process scope is correct because these are
# deploy-time constants, identical for every request -- unlike the
# resolved username, which rides per-request on the config dict.  The
# default here only applies before configuration (i.e. in unit tests).
_SETTINGS: PromptCacheSettings = PromptCacheSettings()


def configure_prompt_cache(config: Mapping[str, Any]) -> None:
    """Install the authored prompt-cache settings for this process.

    Called from the agent config loader, beside the equivalent push of
    prompt/model registration.  An LLM call always requires a loaded
    config, so settings are configured before the first ``create()``.
    """
    global _SETTINGS
    _SETTINGS = PromptCacheSettings.from_config(config)
    logger.info(
        "Prompt cache configured: enabled=%s static_ttl=%s rolling_ttl=%s",
        _SETTINGS.enabled,
        _SETTINGS.static_ttl,
        _SETTINGS.rolling_ttl or "endpoint default",
    )


def get_prompt_cache_settings() -> PromptCacheSettings:
    """Return the settings currently installed for this process."""
    return _SETTINGS


def is_cacheable_model(model: str | None) -> bool:
    """Whether *model* accepts per-block ``cache_control``.

    This is capability detection, not preference: the gateway rejects
    unrecognised fields for other providers, so sending a marker to a
    non-Claude endpoint would fail the request outright.
    """
    if not model:
        return False
    lowered = model.lower()
    return any(marker in lowered for marker in CACHEABLE_MODEL_MARKERS)


def _as_blocks(content: str | list[Any]) -> list[Any]:
    """Return *content* in content-block form, preserving text exactly."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return content


def _mark(block: dict[str, Any], ttl: str | None = None) -> None:
    """Attach a ``cache_control`` marker to *block*, with optional TTL."""
    marker: dict[str, Any] = {"type": "ephemeral"}
    if ttl:
        marker["ttl"] = ttl
    block["cache_control"] = marker


def _is_markable(message: Any) -> bool:
    """Whether a breakpoint may be placed on *message*.

    The only condition is non-empty content.  No role is excluded --
    ``role: "tool"`` accepts block content, which matters because tool
    results are where most of the re-sent tokens live.

    Empty or ``None`` content is never converted: the library's
    ``_fix_empty_assistant_content_in_messages`` runs after this rewrite,
    and turning empty content into a block list would hide it from that
    fix.  Assistant messages carrying only ``tool_calls`` are the common
    case.

    A message whose final block is reasoning is skipped too: the marker
    goes on the last block, and Anthropic rejects ``cache_control`` on a
    thinking block.  Such messages only exist once replayed reasoning has
    been put back on a tool-call message with no text, which is exactly
    the message that was unmarkable before the reasoning arrived.
    """
    if not isinstance(message, dict):
        return False
    content = message.get("content")
    if content is None:
        return False
    if isinstance(content, list) and content:
        last = content[-1]
        if isinstance(last, dict) and last.get("type") in REASONING_BLOCK_TYPES:
            return False
    return bool(content)


def _mark_message(message: dict[str, Any], ttl: str | None) -> bool:
    """Convert *message* to block form and mark its final block."""
    blocks = _as_blocks(message["content"])
    if not blocks:
        return False
    last = blocks[-1]
    if not isinstance(last, dict):
        return False
    message["content"] = blocks
    _mark(last, ttl)
    return True


def split_user_tail_message(messages: list[dict[str, Any]] | None) -> None:
    """Lift the user tail out of the system prompt into its own message.

    The prompt arrives as one string ending in ``<sentinel><user tail>``;
    the tail holds what varies per user or deployment but not per turn
    (skill index, model note).  Moving it into a message of its own puts
    it *after* the cache breakpoint: caching is cumulative from the start
    of the prompt, so left inside the marked message it would join the
    shared cached prefix, and every user would re-write it at full price.

    A message rather than a second content block, because the trace then
    shows request context as request context instead of a stray line
    trailing the system prompt.  Measured on
    ``databricks-claude-sonnet-4-6``: both shapes read the identical
    prefix from cache and neither re-writes it when the tail changes,
    so the choice is free.

    Runs on every request, cached or not -- it is the only thing that
    consumes the sentinel, so skipping it would leak the marker to the
    model.  Idempotent, because a retried ``create()`` re-sends a list
    that has already been split.
    """
    for index, message in enumerate(messages or []):
        tail = _take_tail(message, USER_TAIL_SENTINEL)
        if tail is None:
            continue
        if tail:
            (messages or []).insert(index + 1, {"role": USER_TAIL_ROLE, "content": tail})
        return


def move_context_to_end(messages: list[dict[str, Any]] | None) -> None:
    """Cut the per-request context out of the system prompt and append it last.

    After the history, the context sits behind every breakpoint, so the
    whole history stays a byte-identical prefix from one turn to the next
    while the user, time, memory index and plan change.  The message has
    a system role, which :func:`apply_cache_breakpoints` never marks as a
    rolling breakpoint.

    Must run before :func:`split_user_tail_message`, which would otherwise
    take the context along with the user tail.  Like it, runs on every
    request and is idempotent: a retried ``create()`` re-sends a list
    whose system message no longer carries the sentinel.
    """
    for message in messages or []:
        context = _take_tail(message, CONTEXT_SENTINEL)
        if context is None:
            continue
        if context:
            (messages or []).append({"role": CONTEXT_ROLE, "content": f"{CONTEXT_HEADER}\n\n{context}"})
        return


def _take_tail(message: Any, sentinel: str) -> str | None:
    """Cut *sentinel* and everything after it out of *message*.

    Returns the trailing text, ``""`` when the sentinel was present but
    the tail was empty, and ``None`` when this message does not carry the
    sentinel at all.
    """
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str):
        body, separator, tail = content.partition(sentinel)
        if not separator:
            return None
        message["content"] = body.rstrip()
        return tail.strip()
    if not isinstance(content, list):
        return None
    for position, block in enumerate(content):
        if not isinstance(block, dict) or sentinel not in str(block.get("text", "")):
            continue
        body, _, tail = str(block["text"]).partition(sentinel)
        block["text"] = body.rstrip()
        if not block["text"]:
            del content[position]
        return tail.strip()
    return None


def apply_cache_breakpoints(
    messages: list[dict[str, Any]] | None,
    model: str | None,
    settings: PromptCacheSettings | None = None,
    *,
    max_breakpoints: int = MAX_BREAKPOINTS,
) -> None:
    """Mark up to *max_breakpoints* content blocks with ``cache_control``.

    Mutates *messages* in place, mirroring how ``databricks-openai``
    itself rewrites the outgoing payload.  A no-op for non-Claude models,
    when caching is disabled, or when markers are already present.

    Assumes :func:`split_user_tail_message` has already run, so the system
    message holds nothing per-request and is safe to mark in full.

    Placement, in order:

    1. the first ``system`` / ``developer`` message, on the long-TTL tier
       -- the deployment-stable prefix that pays off across requests;
    2. the last markable message, on the rolling tier -- this is the one
       that pays off *within* a request, since the next call in the agent
       loop reads the history this call just wrote;
    3. the last markable message before that, giving a shorter fallback
       prefix that survives a change in the tail.

    *settings* defaults to the process settings installed by
    :func:`configure_prompt_cache`; passing it explicitly keeps the
    function pure for tests.
    """
    resolved = settings if settings is not None else _SETTINGS
    if not resolved.enabled or not messages or not is_cacheable_model(model):
        return

    # Idempotent: ``create()`` may be retried, and double-marking would
    # both waste breakpoints and risk exceeding the provider limit.
    if any(isinstance(m, dict) and "cache_control" in m for m in messages):
        return
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, list) and any(isinstance(block, dict) and "cache_control" in block for block in content):
            return

    markable = [i for i, message in enumerate(messages) if _is_markable(message)]
    if not markable:
        return

    used = 0

    static_index = next(
        (i for i in markable if messages[i].get("role") in {"system", "developer"}),
        None,
    )
    if static_index is not None and used < max_breakpoints:
        if _mark_message(messages[static_index], resolved.static_ttl):
            used += 1

    # Rolling breakpoints, walking back from the tail.  System-role
    # messages are skipped: the static one already carries a marker, a
    # marker on the user-tail message would spend a breakpoint on a short
    # per-user prefix, and the trailing context changes on every turn, so
    # a marker there would be re-written rather than read.  The rolling
    # breakpoints therefore end on the last history message.
    rolling = [i for i in reversed(markable) if messages[i].get("role") not in {"system", "developer"}]
    for index in rolling[:2]:
        if used >= max_breakpoints:
            break
        if _mark_message(messages[index], resolved.rolling_ttl):
            used += 1


class PromptCachedCompletions(AsyncDatabricksCompletions):  # type: ignore[misc]
    """Completions that add cache breakpoints to the outgoing payload.

    Also the seam for reasoning models (see
    :mod:`payloads.agent.runtime.reasoning`): stored reasoning is put back
    on outgoing tool-call messages, and list ``content`` in the response
    is normalised before the Agents SDK reads it.

    The class name becomes the MLflow span name, so a trace shows
    directly whether a request went through the caching path.
    """

    async def create(self, **kwargs: Any) -> Any:
        messages = kwargs.get("messages")
        model = kwargs.get("model")
        # Order matters: the context and then the user tail have to leave
        # the system message before the breakpoint is placed on it, and
        # replayed reasoning has to be in place before breakpoints are
        # counted, so a marker lands on the message's text rather than
        # being dropped with the content it replaced.
        move_context_to_end(messages)
        split_user_tail_message(messages)
        inject_reasoning(messages, model)
        apply_cache_breakpoints(messages, model)
        response = await super().create(**kwargs)
        # Dispatch on the result, not on ``kwargs["stream"]``: the SDK
        # passes an ``omit`` sentinel when not streaming, and anything
        # that is neither shape (e.g. a raw response) passes through.
        if isinstance(response, ChatCompletion):
            normalize_completion(response, model=model)
        elif isinstance(response, AsyncStream):
            return NormalizedStream(response, model=model)
        return response


class PromptCachedDatabricksOpenAI(AsyncDatabricksOpenAI):  # type: ignore[misc]
    """Databricks client whose completions add cache breakpoints.

    Mirrors the library's own ``chat`` override.  Safe because
    ``AsyncDatabricksOpenAI.chat`` builds a fresh ``AsyncDatabricksChat``
    on every access, so replacing ``completions`` on the returned object
    mutates nothing shared.
    """

    @property
    def chat(self) -> Any:
        chat = super().chat
        chat.completions = PromptCachedCompletions(client=chat.completions._client)
        return chat


def build_llm_client(llm_kind: str) -> PromptCachedDatabricksOpenAI:
    """Return the chat client for *llm_kind*.

    ``model_service`` routes through Unity Gateway
    (``{host}/ai-gateway/mlflow/v1``) and takes the service FQN as the
    model name; anything else talks to serving endpoints.
    """
    if llm_kind == "model_service":
        return PromptCachedDatabricksOpenAI(use_ai_gateway=True)
    return PromptCachedDatabricksOpenAI()


__all__ = [
    "CACHEABLE_MODEL_MARKERS",
    "CONTEXT_HEADER",
    "CONTEXT_ROLE",
    "CONTEXT_SENTINEL",
    "USER_TAIL_ROLE",
    "USER_TAIL_SENTINEL",
    "MAX_BREAKPOINTS",
    "PromptCacheSettings",
    "PromptCachedCompletions",
    "PromptCachedDatabricksOpenAI",
    "apply_cache_breakpoints",
    "build_llm_client",
    "configure_prompt_cache",
    "get_prompt_cache_settings",
    "is_cacheable_model",
    "move_context_to_end",
    "split_user_tail_message",
]
