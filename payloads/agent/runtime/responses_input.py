"""Repair Responses-format input before it reaches ``Runner.run``.

Chat-history replays can break the pairing between ``function_call`` and
``function_call_output`` items, collapse parallel calls that share an
``id``, and flatten list-shaped tool outputs to strings. The helpers here
undo each of those on the way in.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

logger = logging.getLogger(__name__)

# The OpenAI Agents SDK is configured for ``chat_completions`` (see
# :func:`payloads.agent.runtime.runtime_config.init_agent_module`).  Every Responses-format input item we forward
# to ``Runner.run`` is mapped to a Chat Completions message by the SDK:
#
#   function_call          -> {role: "assistant", tool_calls: [{id, ...}]}
#   function_call_output   -> {role: "tool", tool_call_id, content}
#
# The Databricks AI Gateway (and OpenAI directly) reject conversations
# where these halves don't pair up.  Chat-history round-trips through the
# Lakebase-backed UI can drop one half (issue #42), so we sanitize the
# request before handing it to ``Runner.run``.


def _responses_call_id(item: dict[str, Any]) -> str | None:
    """Return the call-side pairing id for an input item, or ``None``."""
    if item.get("type") == "function_call":
        return item.get("call_id")
    return None


def _responses_result_id(item: dict[str, Any]) -> str | None:
    """Return the result-side pairing id for an input item, or ``None``."""
    if item.get("type") == "function_call_output":
        return item.get("call_id")
    return None


# ``ResponsesAgentRequest`` coerces the UI's ``{role, content}`` history items
# into full Responses ``message`` items, filling the unset ``status`` field with
# ``None``.  ``None`` is not a member of the Responses status enum
# (``in_progress`` / ``completed`` / ``incomplete``), and SDK converters that
# validate assistant output messages reject the item outright -- the
# chat-completions converter then falls through to its catch-all and raises
# ``UserError: Unhandled item type or structure``, breaking every multi-turn
# request.  Dropping the null restores the shape the UI actually sent.
def _drop_null_status(item: dict[str, Any]) -> dict[str, Any]:
    """Return *item* without a ``status`` key when it is ``None``."""
    if item.get("type") == "message" and item.get("status") is None:
        return {key: value for key, value in item.items() if key != "status"}
    return item


_ORPHAN_LOG_SAMPLE_SIZE = 5


def _log_orphan_drop(orphan_calls: set[str], orphan_results: set[str]) -> None:
    """Emit one WARNING per request summarising what was dropped.

    Caps the sampled ids to keep the log line bounded; full counts are
    always reported so production telemetry can quantify the rate
    without log explosion.
    """
    sample_calls = sorted(orphan_calls)[:_ORPHAN_LOG_SAMPLE_SIZE]
    sample_results = sorted(orphan_results)[:_ORPHAN_LOG_SAMPLE_SIZE]
    logger.warning(
        "[sanitize] dropped %d orphan tool-call(s) and %d orphan tool-result(s); sample call_ids=%s result_ids=%s",
        len(orphan_calls),
        len(orphan_results),
        sample_calls,
        sample_results,
    )


def sanitize_responses_input(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop orphaned tool-call / tool-result Responses items.

    Pairing keys (Responses item field -> Chat Completions tool_call.id):

      - ``function_call.call_id``       <-> ``function_call_output.call_id``

    Items whose pairing id has no counterpart in *items* are dropped.
    Surviving ``message`` items lose a ``status`` field that is ``None``
    (see :func:`_drop_null_status`); everything else passes through
    untouched and original ordering is preserved.

    Emits a single WARNING per request when anything is dropped; the
    warning carries counts and a sample of orphan ids so production
    log analysis can quantify how often this fires and which side
    (call vs result) is missing.
    """
    call_ids = {cid for item in items if (cid := _responses_call_id(item))}
    result_ids = {rid for item in items if (rid := _responses_result_id(item))}
    paired = call_ids & result_ids

    orphan_calls = call_ids - paired
    orphan_results = result_ids - paired
    if orphan_calls or orphan_results:
        _log_orphan_drop(orphan_calls, orphan_results)

    sanitized: list[dict[str, Any]] = []
    for item in items:
        pair_id = _responses_call_id(item) or _responses_result_id(item)
        if pair_id is not None and pair_id not in paired:
            continue
        sanitized.append(_drop_null_status(item))
    return sanitized


# ---------------------------------------------------------------------------
# Function-call ``id`` uniquification
# ---------------------------------------------------------------------------
# A separate concern from :func:`sanitize_responses_input`, which protects
# the agent from orphan ``function_call`` / ``function_call_output`` pairs
# on the way in.  This helper protects the agent from a different failure
# mode that lives inside the OpenAI Agents SDK itself:
#
# When an MLflow ``ResponsesAgent`` emits multiple tool calls in a single
# assistant turn, every resulting ``function_call`` input item carries the
# *same* Responses-level ``id`` field (the assistant turn's item id),
# distinguished only by their per-call ``call_id``.  Persisted to chat
# history and replayed on the next turn, that input enters
# ``Runner.run`` -> ``deduplicate_input_items_preferring_latest``
# (``agents/run_internal/items.py``), whose ``_dedupe_key`` is
# ``f"id:{type}:{id}"`` -- collapsing all ``function_call`` items sharing
# an ``id`` to the *latest* occurrence and silently dropping the rest.
# Their paired ``function_call_output`` items survive (they carry no
# ``id`` field of their own, only ``call_id``), so the per-iteration
# Chat Completions payload reaches the model with orphan ``tool_result``
# blocks and the Anthropic / Bedrock backend returns ::
#
#     messages.<n>.content.0: unexpected ``tool_use_id`` found in
#     ``tool_result`` blocks: toolu_bdrk_... -- each ``tool_result``
#     block must have a corresponding ``tool_use`` block in the
#     previous message.
#
# Anthropic / Bedrock pair on ``call_id`` (mapped to ``tool_use.id``);
# the Responses-level ``id`` never crosses the wire (the chat-completions
# converter in ``agents/models/chatcmpl_converter.py`` only consults
# ``func_call["call_id"]`` when building each tool_use block).  Rewriting
# duplicate ``id`` values to fresh per-item uuids is therefore safe at
# the wire level; tracing should already correlate on ``call_id`` or
# MLflow span ids.  See ``payloads/agent/runtime/stream_utils.py:101`` for
# the parallel uniquifier the framework applies to outgoing stream
# events.


def uniquify_function_call_ids(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rewrite duplicate ``id`` fields on ``function_call`` items.

    For every ``function_call`` item whose ``id`` collides with another
    ``function_call`` earlier in *items*, replace ``id`` with a fresh
    uuid4 so the OpenAI Agents SDK's ``deduplicate_input_items*`` pass
    does not collapse them.  ``call_id`` -- the field both the chat
    completions adapter and Anthropic / Bedrock pair on -- is never
    touched.  Items of any other type pass through untouched.

    Pure function -- always returns a new list; items whose ``id`` is
    rewritten are shallow-copied so the caller's input is unchanged.
    """
    seen: set[str] = set()
    rewritten: dict[str, str] = {}
    out: list[dict[str, Any]] = []

    for item in items:
        if not isinstance(item, dict) or item.get("type") != "function_call":
            out.append(item)
            continue
        item_id = item.get("id")
        if not isinstance(item_id, str):
            out.append(item)
            continue
        if item_id not in seen:
            seen.add(item_id)
            out.append(item)
            continue
        new_id = str(uuid.uuid4())
        rewritten[item.get("call_id", "<missing call_id>")] = new_id
        replacement = dict(item)
        replacement["id"] = new_id
        out.append(replacement)

    if rewritten:
        logger.warning(
            "[sanitize] rewrote %d duplicate function_call id(s); sample call_id->new_id=%s",
            len(rewritten),
            dict(list(rewritten.items())[:_ORPHAN_LOG_SAMPLE_SIZE]),
        )
    return out


# ---------------------------------------------------------------------------
# Tool-output lists
# ---------------------------------------------------------------------------
# Within a run, a tool result made of content items (MCP tools return a
# list of ``input_text`` items) reaches the model as content blocks.  The
# chat UI's history sends the same result back as the JSON text of that
# list, which the model sees as an escaped string: different tokens, so the
# prompt cache stops matching at the previous turn's first tool result.

_TOOL_OUTPUT_ITEM_TYPES = frozenset({"input_text", "input_image", "input_file"})


def _as_tool_output_list(output: Any) -> list[dict[str, Any]] | None:
    """*output* as a list of tool-output content items, or ``None`` if it is not one."""
    if not isinstance(output, str) or not output.startswith("["):
        return None
    try:
        parsed = json.loads(output)
    except ValueError:
        return None
    if not isinstance(parsed, list) or not parsed:
        return None
    if all(isinstance(entry, dict) and entry.get("type") in _TOOL_OUTPUT_ITEM_TYPES for entry in parsed):
        return parsed
    return None


def restore_tool_output_lists(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """*items* with JSON-encoded tool-output lists restored to the list they encode.

    Only a ``function_call_output`` whose ``output`` string decodes to a
    non-empty list of tool-output content items changes, so it reaches the
    model as it did in its own run; everything else passes through.
    Changed items are shallow-copied, so the caller's input is unchanged.
    """
    restored: list[dict[str, Any]] = []
    for item in items:
        output_list = (
            _as_tool_output_list(item.get("output"))
            if isinstance(item, dict) and item.get("type") == "function_call_output"
            else None
        )
        restored.append(item if output_list is None else {**item, "output": output_list})
    return restored
