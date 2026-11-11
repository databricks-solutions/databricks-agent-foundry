"""
Tool Routing Accuracy Scorer
==============================
Code-based scorer that checks whether the agent routed queries to
the expected tools.  Compares actual tool call span names from
the trace against ``expectations["expected_tools"]``.

**Dev evaluation only** — requires ``expectations`` with ground-truth
tool names, which is not available in production monitoring.  Do not
register this scorer for production monitoring.

**Lazy skill loading (issue #98)**: when the agent runs in
``skill_loading: lazy`` mode, skill activation surfaces as a
``_load_skill`` tool span carrying ``skill.loaded=<skill_id>`` as an
attribute.  ``_extract_tool_names`` additively emits that ``skill_id``
alongside the literal ``_load_skill`` name so the same
``expected_tools: ["<skill_id>"]`` ground-truth row works in BOTH
eager and lazy modes.  Substring matching on the literal tool name
is preserved for backward compatibility.
"""

from __future__ import annotations

from typing import Any

from mlflow.entities import Feedback
from mlflow.entities.trace import Trace
from mlflow.genai.scorers import scorer

_TOOL_SPAN_TYPES = frozenset({"TOOL", "FUNCTION", "RETRIEVER"})

# Must match ``LOAD_SKILL_TOOL_NAME`` and ``SKILL_LOADED_ATTRIBUTE`` in
# ``bricks/agent/lazy_skills.py``.  Duplicated as plain strings here
# rather than imported to keep the evaluation brick free of an agent
# runtime dependency (the scorers run in MLflow harnesses that may
# not have the agent's deps installed).
_LOAD_SKILL_SPAN_NAME = "_load_skill"
_SKILL_LOADED_ATTRIBUTE = "skill.loaded"


def _extract_loaded_skill_id(span: Any) -> str | None:
    """Pull the ``skill_id`` argument out of a ``_load_skill`` span.

    Tries the OTel attribute first (set via ``span.set_attribute``)
    and falls back to ``span.inputs["skill_id"]`` (set via
    ``span.set_inputs``) when the attribute is missing -- some MLflow
    versions strip / namespace attributes differently when serialising
    traces to UC tables.  Returns ``None`` when neither carries a
    usable value, so the caller can skip the additive emission
    silently.
    """
    attributes = getattr(span, "attributes", None) or {}
    raw = attributes.get(_SKILL_LOADED_ATTRIBUTE)
    if isinstance(raw, str) and raw:
        return raw
    inputs = getattr(span, "inputs", None) or {}
    raw = inputs.get("skill_id") if isinstance(inputs, dict) else None
    if isinstance(raw, str) and raw:
        return raw
    return None


def _extract_tool_names(trace: Trace | None) -> list[str]:
    """Extract unique tool / retriever span names invoked during the trace.

    RETRIEVER is included so vector-search skills (e.g. ``docs_search``)
    count as tool invocations for routing purposes.

    Additive lazy-mode extension: every ``_load_skill`` span also
    contributes its ``skill_id`` argument as a synthetic "tool name"
    so ``expected_tools`` ground truth can name the skill that should
    have been loaded -- the symmetric counterpart to naming a
    skill-owned tool in eager mode.
    """
    if trace is None:
        return []
    tool_names: list[str] = []
    try:
        spans = trace.search_spans()
    except Exception:
        return []
    for span in spans:
        span_type = getattr(span, "span_type", None)
        if span_type and str(span_type).upper() in _TOOL_SPAN_TYPES:
            tool_names.append(span.name)
            if span.name == _LOAD_SKILL_SPAN_NAME:
                skill_id = _extract_loaded_skill_id(span)
                if skill_id:
                    tool_names.append(skill_id)
    return list(dict.fromkeys(tool_names))


@scorer
def tool_routing_accuracy(
    trace: Trace | None,
    expectations: dict[str, Any] | None = None,
) -> Feedback | None:
    """Score whether the agent called the expected tools.

    Expects ``expectations["expected_tools"]`` to be a list of tool
    name substrings (case-insensitive).  A tool call matches if any
    expected substring appears in the actual tool span name.

    Returns ``True`` if all expected tools were invoked, ``False`` otherwise.
    Returns ``None`` (harness records no assessment for this row) when
    the row has no ``expected_tools`` key, so the scorer opts out rather
    than failing the row.
    """
    if not expectations or "expected_tools" not in expectations:
        return None

    expected = [t.lower() for t in expectations["expected_tools"]]
    actual = [t.lower() for t in _extract_tool_names(trace)]

    matched = []
    missing = []
    for exp in expected:
        if any(exp in act for act in actual):
            matched.append(exp)
        else:
            missing.append(exp)

    extra = [a for a in actual if not any(e in a for e in expected)]

    if not missing:
        return Feedback(
            value=True,
            rationale=(
                f"All expected tools invoked. "
                f"Matched: {matched}. Actual calls: {actual}." + (f" Extra calls: {extra}." if extra else "")
            ),
        )

    return Feedback(
        value=False,
        rationale=(f"Missing expected tools: {missing}. Matched: {matched}. Actual calls: {actual}."),
    )
