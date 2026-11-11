"""
Response Quality Scorer (Composite)
=====================================
Code-based scorer that returns multiple ``Feedback`` objects evaluating
different quality dimensions of the agent's response:

  - **response_length**: Whether the response meets minimum length
  - **has_sources**: Whether the response references source data
  - **latency_ok**: Whether end-to-end latency is within threshold

These are deterministic, code-based checks that complement the built-in
LLM judges (Safety, RelevanceToQuery, etc.).

Works with both ``mlflow.genai.evaluate()`` and production monitoring.
"""

from __future__ import annotations

from typing import Any

from mlflow.entities import Feedback
from mlflow.entities.trace import Trace
from mlflow.genai.scorers import scorer

MIN_RESPONSE_LENGTH = 20
MAX_LATENCY_SECONDS = 30.0
SOURCE_INDICATORS = ["source", "table", "catalog", "schema", "according to", "based on"]


@scorer
def response_quality(
    inputs: dict[str, Any] | None = None,
    outputs: Any | None = None,
    trace: Trace | None = None,
) -> list[Feedback]:
    """Composite quality scorer returning multiple feedback dimensions."""
    feedback: list[Feedback] = []

    response_text = _extract_response_text(outputs)
    feedback.append(_check_response_length(response_text))
    feedback.append(_check_has_sources(response_text))

    if trace is not None:
        feedback.append(_check_latency(trace))

    return feedback


def _extract_response_text(outputs: Any) -> str:
    """Pull plain text from various output formats."""
    if outputs is None:
        return ""
    if isinstance(outputs, str):
        return outputs
    if isinstance(outputs, dict):
        for key in ("response", "text", "content", "output"):
            if key in outputs:
                return str(outputs[key])
        output_items = outputs.get("output", [])
        if isinstance(output_items, list):
            parts = []
            for item in output_items:
                if isinstance(item, dict):
                    for c in item.get("content", []):
                        if isinstance(c, dict):
                            parts.append(c.get("text", ""))
                        elif isinstance(c, str):
                            parts.append(c)
            return " ".join(parts)
    return str(outputs)


def _check_response_length(text: str) -> Feedback:
    word_count = len(text.split())
    if word_count >= MIN_RESPONSE_LENGTH:
        return Feedback(
            name="response_length",
            value=True,
            rationale=f"Response has {word_count} words (minimum: {MIN_RESPONSE_LENGTH}).",
        )
    return Feedback(
        name="response_length",
        value=False,
        rationale=f"Response too short: {word_count} words (minimum: {MIN_RESPONSE_LENGTH}).",
    )


def _check_has_sources(text: str) -> Feedback:
    text_lower = text.lower()
    found = [ind for ind in SOURCE_INDICATORS if ind in text_lower]
    if found:
        return Feedback(
            name="has_sources",
            value=True,
            rationale=f"Response references sources: {found}.",
        )
    return Feedback(
        name="has_sources",
        value=False,
        rationale="No source indicators found in the response.",
    )


def _check_latency(trace: Trace) -> Feedback:
    root_span = trace.data.spans[0] if trace.data.spans else None
    if root_span is None:
        return Feedback(
            name="latency_ok",
            value=False,
            rationale="No root span found in trace to measure latency.",
        )

    # mlflow types ``end_time_ns`` / ``start_time_ns`` as ``int | None``
    # (in-progress spans haven't ended yet).  The trace passed to a
    # scorer is always the completed trace, but type-narrow defensively
    # so callers see the expected ``Feedback`` shape if the spans
    # somehow surfaced incomplete.
    if root_span.end_time_ns is None or root_span.start_time_ns is None:
        return Feedback(
            name="latency_ok",
            value=False,
            rationale="Root span timestamps incomplete; cannot measure latency.",
        )

    duration_ns = root_span.end_time_ns - root_span.start_time_ns
    duration_s = duration_ns / 1e9

    if duration_s <= MAX_LATENCY_SECONDS:
        return Feedback(
            name="latency_ok",
            value=True,
            rationale=f"Latency {duration_s:.2f}s within {MAX_LATENCY_SECONDS}s threshold.",
        )
    return Feedback(
        name="latency_ok",
        value=False,
        rationale=f"Latency {duration_s:.2f}s exceeds {MAX_LATENCY_SECONDS}s threshold.",
    )
