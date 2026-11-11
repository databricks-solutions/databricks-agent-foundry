"""Client-side clearing of stale tool results from the model input.

Long-running conversations accumulate large ``function_call_output`` payloads
that the model rarely needs verbatim once it has acted on them.  This module
replaces older outputs with a short placeholder immediately before each model
call, via ``RunConfig.call_model_input_filter``, so the behaviour is
identical across Claude, GPT and open-weights endpoints served through the
OpenAI-compatible gateway.

The full transcript in Lakebase / the UI is untouched: only the in-flight
message list sent to the model is rewritten.

Two independent conditions arm the clearing pass, and either is enough:

*savings available* (``clear_at_least_chars``) -- how much a pass would
recover, which keeps small wins from repeatedly invalidating the rolling
prompt cache; and *occupancy* (``clear_above_input_tokens``) -- how full
the outbound request already is, which is the safety valve.  The second
exists because the first cannot express it: a conversation whose growth
is prose rather than tool output never accumulates enough clearable
material to trip a savings threshold, and nothing else in the framework
bounds context growth, so it would walk into the provider's hard
``prompt is too long`` error with no backstop.  See issue #424.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any, Final

from agents import RunConfig

from payloads.agent.runtime.context_occupancy import OccupancyRecorder
from payloads.agent.runtime.lazy_skills import LOAD_SKILL_TOOL_NAME
from payloads.agent.runtime.memory import (
    FORGET_TOOL_NAME,
    LIST_MEMORIES_TOOL_NAME,
    RECALL_TOOL_NAME,
    REMEMBER_TOOL_NAME,
)
from payloads.agent.runtime.plan import DELETE_PLAN_TOOL_NAME, UPDATE_PLAN_TOOL_NAME

logger = logging.getLogger(__name__)

CLEARING_PLACEHOLDER: Final = "[Tool result cleared to conserve context. Re-call the tool if needed.]"

# Savings-available trigger.  Set high because *any* clearing pass rewrites
# ``function_call_output`` items in the middle of the message list, which
# breaks the prompt-cache prefix at the first cleared item and re-bills
# everything after it as a write instead of a 0.1x read.  A low value trades
# most of the cache discount for a few thousand tokens of headroom on a
# window that is nowhere near full; occupancy is the trigger that matters.
DEFAULT_CLEAR_AT_LEAST_CHARS: Final = 300_000

# Occupancy trigger, in estimated prompt tokens: the in-turn emergency valve,
# above the compaction trigger, since compaction only runs between turns.
# Sized for the 200k window of the Claude endpoints this framework targets,
# at three quarters full, which leaves room for the reply plus one more
# oversized tool result.  Endpoints with a smaller window (128k and below)
# need this lowered.
DEFAULT_CLEAR_ABOVE_INPUT_TOKENS: Final = 150_000

# Divisor for the character-to-token estimate.  Well below the ~4 typical
# of English prose: tool output is JSON, SQL and IDs, which tokenise worse,
# and a Claude 5.5 agent chat with thinking measured 2.0-2.5 characters per
# billed token.  A safety valve should over-estimate occupancy so it arms
# early rather than late.  An estimate is enough here -- no tokenizer is
# available for every model family behind the OpenAI-compatible gateway, and
# a valve that fires at 75% of the window does not need to be accurate to
# the token.
CHARS_PER_TOKEN: Final = 2.0

# Input item types the model produced.  An output followed by one of these
# (or by an assistant message) has been read; one that is not is unseen.
_MODEL_ITEM_TYPES: Final = frozenset({"function_call", "reasoning"})

# Built-in tools whose outputs are never cleared and never take a
# ``keep_tool_uses`` slot: plan and memory replies are short bookkeeping,
# and a loaded skill body holds instructions the model is still following.
_EXEMPT_TOOL_NAMES: Final = frozenset(
    {
        UPDATE_PLAN_TOOL_NAME,
        DELETE_PLAN_TOOL_NAME,
        REMEMBER_TOOL_NAME,
        RECALL_TOOL_NAME,
        FORGET_TOOL_NAME,
        LIST_MEMORIES_TOOL_NAME,
        LOAD_SKILL_TOOL_NAME,
    }
)


@dataclass(frozen=True)
class ToolResultClearingSettings:
    """Resolved ``spec.runtime.context.tool_result_clearing`` values."""

    enabled: bool = True
    keep_tool_uses: int = 3
    clear_at_least_chars: int = DEFAULT_CLEAR_AT_LEAST_CHARS
    clear_above_input_tokens: int = DEFAULT_CLEAR_ABOVE_INPUT_TOKENS

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> ToolResultClearingSettings:
        """Build settings from the runtime config dict."""
        defaults = cls()
        enabled = config.get("TOOL_RESULT_CLEARING_ENABLED")
        keep = config.get("TOOL_RESULT_CLEARING_KEEP_TOOL_USES")
        clear_at_least = config.get("TOOL_RESULT_CLEARING_CLEAR_AT_LEAST_CHARS")
        clear_above = config.get("TOOL_RESULT_CLEARING_CLEAR_ABOVE_INPUT_TOKENS")
        return cls(
            enabled=defaults.enabled if enabled is None else bool(enabled),
            keep_tool_uses=defaults.keep_tool_uses if keep is None else int(keep),
            clear_at_least_chars=(defaults.clear_at_least_chars if clear_at_least is None else int(clear_at_least)),
            clear_above_input_tokens=(defaults.clear_above_input_tokens if clear_above is None else int(clear_above)),
        )


_SETTINGS: ToolResultClearingSettings = ToolResultClearingSettings()


def configure_tool_result_clearing(config: Mapping[str, Any]) -> None:
    """Install authored clearing settings for this process."""
    global _SETTINGS
    _SETTINGS = ToolResultClearingSettings.from_config(config)
    logger.info(
        "Tool result clearing configured: enabled=%s keep_tool_uses=%s "
        "clear_at_least_chars=%s clear_above_input_tokens=%s",
        _SETTINGS.enabled,
        _SETTINGS.keep_tool_uses,
        _SETTINGS.clear_at_least_chars,
        _SETTINGS.clear_above_input_tokens or "off",
    )


def get_tool_result_clearing_settings() -> ToolResultClearingSettings:
    """Return the settings currently installed for this process."""
    return _SETTINGS


def _tokens_from_chars(chars: int) -> int:
    """Convert a character count to an estimated token count."""
    return int(chars / CHARS_PER_TOKEN)


def _request_chars(items: list[Any], instructions: str | None, tool_schema_chars: int = 0) -> int:
    """Characters the outbound request carries, across all of its parts.

    ``instructions`` is counted because that is where the system prompt,
    the assembled skill bodies and the data-universe block live -- a large,
    fixed share of occupancy that the message list alone does not show.
    Only read, never rewritten: it is the cached static prefix.
    *tool_schema_chars* covers the tool definitions sent with every call,
    which the message list does not show either.
    """
    total = len(instructions or "") + tool_schema_chars
    for item in items:
        total += len(item) if isinstance(item, str) else len(str(item))
    return total


def _tool_chars(tool: Any) -> int:
    schema = getattr(tool, "params_json_schema", None)
    name = getattr(tool, "name", "") or ""
    description = getattr(tool, "description", "") or ""
    return len(name) + len(description) + (len(json.dumps(schema, default=str)) if schema else 0)


async def tool_schema_chars(agent: Any, context: Any) -> int:
    """Characters of the tool definitions *agent* sends with every model call.

    Call inside the run's MCP scope: MCP tool lists are cached per server
    for the run, so listing them here adds no round trip.  A listing
    failure counts as zero, because the run itself surfaces the error.
    """
    from agents import RunContextWrapper

    try:
        tools = await agent.get_all_tools(RunContextWrapper(context=context))
    except Exception:
        logger.warning("Could not list tools for the occupancy estimate; counting none", exc_info=True)
        return 0
    return sum(_tool_chars(tool) for tool in tools)


def estimate_input_tokens(
    items: list[Any],
    instructions: str | None = None,
    tool_schema_chars: int = 0,
) -> int:
    """Estimate the prompt tokens an outbound request would bill.

    Measured on the request itself rather than read back from the previous
    response's ``usage``, for three reasons: this function stays pure, it
    needs no state shared across modules, and it has no lag -- a single
    30k-character tool result can arrive on the very call that would
    otherwise cross the window limit.
    """
    return _tokens_from_chars(_request_chars(items, instructions, tool_schema_chars))


def _build_call_id_to_tool_name(items: list[Any]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict) or item.get("type") != "function_call":
            continue
        call_id = item.get("call_id") or item.get("id")
        name = item.get("name")
        if call_id and isinstance(name, str) and name:
            mapping[str(call_id)] = name
    return mapping


def _is_model_item(item: Any) -> bool:
    return isinstance(item, dict) and (item.get("type") in _MODEL_ITEM_TYPES or item.get("role") == "assistant")


def _last_model_position(items: list[Any]) -> int:
    """Index of the last item the model produced, or -1."""
    return max((index for index, item in enumerate(items) if _is_model_item(item)), default=-1)


def _recent_round_positions(items: list[Any], keep_rounds: int, clearable: Collection[int]) -> set[int]:
    """*clearable* positions answering the last *keep_rounds* model calls that have any.

    One round is everything the model asked for in one call, so a batch of
    parallel results counts once.  A round with nothing clearable, such as
    a plan update, takes no slot.
    """
    round_of: dict[int, int] = {}
    last_model = -1
    for index, item in enumerate(items):
        if _is_model_item(item):
            last_model = index
        elif index in clearable:
            round_of[index] = last_model
    recent = set(sorted(set(round_of.values()))[-keep_rounds:]) if keep_rounds else set()
    return {index for index, model_index in round_of.items() if model_index in recent}


def _placeholder_for_tool(tool_name: str | None) -> str:
    if not tool_name:
        return CLEARING_PLACEHOLDER
    return f"[Tool result cleared ({tool_name}) to conserve context. Re-call the tool if needed.]"


def _shrink_function_call_output(
    item: dict[str, Any],
    call_id_to_name: Mapping[str, str],
) -> dict[str, Any] | None:
    """Return a shallow copy with a placeholder output, or ``None`` if unchanged."""
    output = item.get("output", "")
    output_str = output if isinstance(output, str) else str(output)
    placeholder = _placeholder_for_tool(call_id_to_name.get(_call_id(item)))
    if len(placeholder) >= len(output_str):
        return None
    cleared = dict(item)
    cleared["output"] = placeholder
    return cleared


def _call_id(item: Mapping[str, Any]) -> str:
    return str(item.get("call_id") or item.get("id") or "")


def _is_clearable(item: dict[str, Any], call_id_to_name: Mapping[str, str]) -> bool:
    """Whether clearing *item* would save anything: not exempt, and longer than its placeholder."""
    if call_id_to_name.get(_call_id(item)) in _EXEMPT_TOOL_NAMES:
        return False
    return _shrink_function_call_output(item, call_id_to_name) is not None


def _shrunk(
    items: list[Any], positions: Collection[int], call_id_to_name: Mapping[str, str]
) -> tuple[dict[int, dict[str, Any]], dict[int, int]]:
    """Placeholders for the outputs at *positions*, and the characters each saves."""
    replacements: dict[int, dict[str, Any]] = {}
    savings: dict[int, int] = {}
    for index in positions:
        replacement = _shrink_function_call_output(items[index], call_id_to_name)
        if replacement is not None:
            replacements[index] = replacement
            savings[index] = len(str(items[index].get("output", ""))) - len(str(replacement["output"]))
    return replacements, savings


def apply_tool_result_clearing(
    items: list[Any],
    *,
    keep_tool_uses: int,
    clear_at_least_chars: int = DEFAULT_CLEAR_AT_LEAST_CHARS,
    clear_above_input_tokens: int = DEFAULT_CLEAR_ABOVE_INPUT_TOKENS,
    instructions: str | None = None,
    tool_schema_chars: int = 0,
    cleared_call_ids: Collection[str] = (),
) -> list[Any]:
    """Clear old outputs when either trigger arms.

    Clearing happens when a pass would recover at least *clear_at_least_chars*
    characters, **or** when estimated occupancy has reached
    *clear_above_input_tokens*.  Zero disables a trigger.  Either trigger
    clears every eligible output: a pass breaks the prompt cache from the
    first cleared item on, so one large pass costs one cache rewrite where
    trimming just under the threshold would cost one on every later call.

    Outputs in *cleared_call_ids* -- cleared by an earlier pass of the same
    run -- are always cleared again, and both triggers measure the request
    with them cleared, so between passes every call sends the same prefix.

    The outputs of the last *keep_tool_uses* rounds with something to
    clear stay (see :func:`_recent_round_positions`).  Outputs of the
    built-in plan, memory and skill tools are never cleared.  An output
    the model has not read yet -- nothing it produced follows it, as with
    a fresh batch of parallel results -- is never cleared either.
    """
    if keep_tool_uses < 0:
        raise ValueError(f"keep_tool_uses must be >= 0, got {keep_tool_uses}")
    if clear_at_least_chars < 0:
        raise ValueError(f"clear_at_least_chars must be >= 0, got {clear_at_least_chars}")
    if clear_above_input_tokens < 0:
        raise ValueError(f"clear_above_input_tokens must be >= 0, got {clear_above_input_tokens}")
    if not items:
        return items

    output_positions = [
        index
        for index, item in enumerate(items)
        if isinstance(item, dict) and item.get("type") == "function_call_output"
    ]
    if len(output_positions) <= keep_tool_uses:
        return items

    seen_before = _last_model_position(items)
    call_id_to_name = _build_call_id_to_tool_name(items)
    clearable = {index for index in output_positions if _is_clearable(items[index], call_id_to_name)}
    kept_positions = _recent_round_positions(items, keep_tool_uses, clearable)
    sticky_positions = {index for index in clearable if _call_id(items[index]) in cleared_call_ids}
    fresh_positions = [
        index
        for index in sorted(clearable)
        if index < seen_before and index not in kept_positions and index not in sticky_positions
    ]
    sticky, sticky_savings = _shrunk(items, sticky_positions, call_id_to_name)
    fresh, fresh_savings = _shrunk(items, fresh_positions, call_id_to_name)

    # Occupancy is measured with earlier passes applied but before this one:
    # the question is how big the request would be if the valve stayed shut.
    request_chars = (
        _request_chars(items, instructions, tool_schema_chars) - sum(sticky_savings.values())
        if clear_above_input_tokens
        else 0
    )
    over_occupancy = clear_above_input_tokens > 0 and _tokens_from_chars(request_chars) >= clear_above_input_tokens
    armed = bool(fresh) and (sum(fresh_savings.values()) >= clear_at_least_chars or over_occupancy)
    replacements = {**sticky, **fresh} if armed else sticky
    if not replacements:
        return items
    if not armed:
        return [replacements.get(index, item) for index, item in enumerate(items)]
    chars_saved = sum(fresh_savings.values())

    logger.debug(
        "Tool result clearing: cleared %d output(s), saved %d chars, keep_tool_uses=%d occupancy_triggered=%s",
        len(fresh),
        chars_saved,
        keep_tool_uses,
        over_occupancy,
    )
    if over_occupancy:
        remaining = _tokens_from_chars(request_chars - chars_saved)
        if remaining >= clear_above_input_tokens:
            logger.warning(
                "Tool result clearing fired on occupancy but the request is "
                "still ~%d estimated tokens against a %d threshold: this "
                "conversation's growth is not dominated by tool output, so "
                "clearing alone cannot bound it.",
                remaining,
                clear_above_input_tokens,
            )
    return [replacements.get(index, item) for index, item in enumerate(items)]


class _ClearingFilter:
    """One run's ``CallModelInputFilter``: an output once cleared stays cleared for the run."""

    def __init__(
        self, settings: ToolResultClearingSettings, recorder: OccupancyRecorder | None, tool_schema_chars: int
    ) -> None:
        self._settings = settings
        self._recorder = recorder
        self._tool_schema_chars = tool_schema_chars
        self._cleared_call_ids: set[str] = set()

    def _clear(self, items: list[Any], instructions: str | None) -> list[Any]:
        settings = self._settings
        if not settings.enabled:
            return items
        cleared = apply_tool_result_clearing(
            items,
            keep_tool_uses=settings.keep_tool_uses,
            clear_at_least_chars=settings.clear_at_least_chars,
            clear_above_input_tokens=settings.clear_above_input_tokens,
            instructions=instructions,
            tool_schema_chars=self._tool_schema_chars,
            cleared_call_ids=self._cleared_call_ids,
        )
        self._cleared_call_ids.update(_call_id(new) for new, old in zip(cleared, items, strict=True) if new is not old)
        return cleared

    def __call__(self, data: Any) -> Any:
        from agents.run_config import ModelInputData

        model_data = data.model_data
        input_list = list(model_data.input)
        if self._recorder is not None:
            self._recorder.record(estimate_input_tokens(input_list, model_data.instructions, self._tool_schema_chars))
        cleared_input = self._clear(input_list, model_data.instructions)
        if cleared_input is input_list:
            return model_data
        if self._recorder is not None:
            self._recorder.record_sent(
                estimate_input_tokens(cleared_input, model_data.instructions, self._tool_schema_chars)
            )
        return ModelInputData(input=cleared_input, instructions=model_data.instructions)


def build_call_model_input_filter(
    settings: ToolResultClearingSettings,
    recorder: OccupancyRecorder | None = None,
    tool_schema_chars: int = 0,
) -> Any:
    """Return a ``CallModelInputFilter`` for one run with *settings*.

    With *recorder*, every call's occupancy is recorded before clearing,
    whether or not clearing is enabled: compaction acts on it, and the
    next turn's request starts from the uncleared history (#672).
    *tool_schema_chars* (see :func:`tool_schema_chars`) counts towards
    both the occupancy trigger and the recorded estimate.  Build one per
    run: the filter remembers what it cleared.
    """
    return _ClearingFilter(settings, recorder, tool_schema_chars)


def run_config_for(
    config: Mapping[str, Any],
    recorder: OccupancyRecorder | None = None,
    tool_schema_chars: int = 0,
) -> RunConfig | None:
    """Build the ``RunConfig`` for one run, or ``None`` when it has nothing to do.

    The filter is installed when clearing is enabled or occupancy is being
    recorded; with neither, the run gets no filter at all.
    """
    settings = ToolResultClearingSettings.from_config(config)
    if not settings.enabled and recorder is None:
        return None
    return RunConfig(call_model_input_filter=build_call_model_input_filter(settings, recorder, tool_schema_chars))


__all__ = [
    "CHARS_PER_TOKEN",
    "CLEARING_PLACEHOLDER",
    "DEFAULT_CLEAR_ABOVE_INPUT_TOKENS",
    "DEFAULT_CLEAR_AT_LEAST_CHARS",
    "ToolResultClearingSettings",
    "apply_tool_result_clearing",
    "build_call_model_input_filter",
    "configure_tool_result_clearing",
    "estimate_input_tokens",
    "get_tool_result_clearing_settings",
    "run_config_for",
    "tool_schema_chars",
]
