"""
Offline unit tests for the evaluation scorer catalog.

These tests verify scorer function signatures, return types, and logic
without requiring MLflow or a Databricks workspace.  We mock the Trace
and Feedback objects with minimal stand-ins.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

import pytest

# ---------------------------------------------------------------------------
# Minimal stand-ins for MLflow types (avoids importing mlflow in tests)
# ---------------------------------------------------------------------------


@dataclass
class FakeSpan:
    name: str
    span_type: str | None = None
    outputs: Any = None
    start_time_ns: int = 0
    end_time_ns: int = 1_000_000_000
    # Lazy-skill loading hooks (issue #98 PR 2/2): the routing scorer
    # reads ``attributes["skill.loaded"]`` first and falls back to
    # ``inputs["skill_id"]`` when extracting the skill_id from a
    # ``_load_skill`` span.  Defaults keep older tests untouched.
    attributes: dict[str, Any] = field(default_factory=dict)
    inputs: Any = None


@dataclass
class FakeTraceData:
    spans: list[FakeSpan] = field(default_factory=list)


@dataclass
class FakeTrace:
    data: FakeTraceData = field(default_factory=FakeTraceData)

    def search_spans(self, **kwargs: Any) -> list[FakeSpan]:
        return self.data.spans


@dataclass
class FakeFeedback:
    value: Any = None
    rationale: str = ""
    name: str | None = None


# ---------------------------------------------------------------------------
# Scorer-call test helpers
# ---------------------------------------------------------------------------
#
# The ``@scorer`` decorator wraps each scorer into
# ``mlflow.genai.scorers.Scorer``, whose ``__call__`` is typed as
# ``(trace: Trace | None, ...) -> int | float | bool | str | Feedback
# | list[Feedback]``.  Tests pass our duck-typed ``FakeTrace`` and
# only ever exercise the ``Feedback`` / ``list[Feedback]`` branches
# (the scorer functions themselves are typed ``Feedback | None`` and
# ``list[Feedback]`` respectively).  The helpers below take the
# ``cast`` once so test bodies can read ``result.value`` /
# ``result.rationale`` directly without per-line ignores.


def _call_scalar(scorer: Any, **kwargs: Any) -> FakeFeedback:
    """Invoke a scalar scorer (``Feedback | None``) and assert a
    non-None result, narrowing to our test stand-in.

    Most tests intentionally exercise the "produced feedback" branch
    and immediately read ``result.value`` / ``result.rationale``.
    For the few opt-out tests that expect ``None``, use
    :func:`_call_scalar_or_none` instead.

    Also absorbs the ``Trace | None`` vs. ``FakeTrace`` mismatch on
    the ``trace=`` kwarg so individual call sites stay terse.
    """
    result = cast("FakeFeedback | None", scorer(**kwargs))
    assert result is not None, "scorer returned None unexpectedly"
    return result


def _call_scalar_or_none(scorer: Any, **kwargs: Any) -> FakeFeedback | None:
    """Variant of :func:`_call_scalar` for tests that exercise the
    opt-out (``None``) branch of a scalar scorer's return contract.
    """
    return cast("FakeFeedback | None", scorer(**kwargs))


def _call_list(scorer: Any, **kwargs: Any) -> list[FakeFeedback]:
    """Invoke a multi-feedback scorer (``list[Feedback]``) and
    narrow the mlflow union return type to ``list[FakeFeedback]``.
    """
    return cast("list[FakeFeedback]", scorer(**kwargs))


# ---------------------------------------------------------------------------
# Patch mlflow imports before importing scorers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_mlflow(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace mlflow.entities and mlflow.genai.scorers with fakes.

    ``types.ModuleType`` instances are dynamic at runtime but mypy
    treats them as opaque: assigning ``mod.X = Y`` directly trips
    ``attr-defined``.  ``setattr`` is the documented escape hatch for
    dynamic module-attribute writes and keeps the patch readable.
    """
    import types

    mlflow_mod = types.ModuleType("mlflow")
    entities_mod = types.ModuleType("mlflow.entities")
    trace_mod = types.ModuleType("mlflow.entities.trace")
    genai_mod = types.ModuleType("mlflow.genai")
    scorers_mod = types.ModuleType("mlflow.genai.scorers")

    setattr(entities_mod, "Feedback", FakeFeedback)
    setattr(entities_mod, "Trace", FakeTrace)
    setattr(trace_mod, "Trace", FakeTrace)
    setattr(scorers_mod, "scorer", lambda fn=None, **kw: fn if fn else (lambda f: f))
    setattr(scorers_mod, "Feedback", FakeFeedback)

    setattr(mlflow_mod, "entities", entities_mod)
    setattr(mlflow_mod, "genai", genai_mod)
    setattr(genai_mod, "scorers", scorers_mod)

    monkeypatch.setitem(__import__("sys").modules, "mlflow", mlflow_mod)
    monkeypatch.setitem(__import__("sys").modules, "mlflow.entities", entities_mod)
    monkeypatch.setitem(__import__("sys").modules, "mlflow.entities.trace", trace_mod)
    monkeypatch.setitem(__import__("sys").modules, "mlflow.genai", genai_mod)
    monkeypatch.setitem(__import__("sys").modules, "mlflow.genai.scorers", scorers_mod)


# ---------------------------------------------------------------------------
# Routing scorer tests
# ---------------------------------------------------------------------------


class TestToolRoutingAccuracy:
    def test_all_expected_tools_found(self) -> None:
        from bricks.evaluation.scorers.routing import tool_routing_accuracy

        trace = FakeTrace(
            data=FakeTraceData(
                spans=[
                    FakeSpan(name="genie_query", span_type="TOOL"),
                    FakeSpan(name="vector_search_retrieve", span_type="TOOL"),
                ]
            )
        )
        result = _call_scalar(
            tool_routing_accuracy,
            trace=trace,
            expectations={"expected_tools": ["genie", "vector_search"]},
        )
        assert result.value is True

    def test_missing_expected_tool(self) -> None:
        from bricks.evaluation.scorers.routing import tool_routing_accuracy

        trace = FakeTrace(
            data=FakeTraceData(
                spans=[
                    FakeSpan(name="genie_query", span_type="TOOL"),
                ]
            )
        )
        result = _call_scalar(
            tool_routing_accuracy,
            trace=trace,
            expectations={"expected_tools": ["genie", "vector_search"]},
        )
        assert result.value is False
        assert "vector_search" in result.rationale

    def test_no_expectations_skips(self) -> None:
        from bricks.evaluation.scorers.routing import tool_routing_accuracy

        trace = FakeTrace()
        result = _call_scalar_or_none(tool_routing_accuracy, trace=trace, expectations=None)
        assert result is None

    def test_empty_expected_tools(self) -> None:
        from bricks.evaluation.scorers.routing import tool_routing_accuracy

        trace = FakeTrace(
            data=FakeTraceData(
                spans=[
                    FakeSpan(name="some_tool", span_type="TOOL"),
                ]
            )
        )
        result = _call_scalar(
            tool_routing_accuracy,
            trace=trace,
            expectations={"expected_tools": []},
        )
        assert result.value is True

    def test_retriever_span_counts_as_tool(self) -> None:
        from bricks.evaluation.scorers.routing import tool_routing_accuracy

        trace = FakeTrace(
            data=FakeTraceData(
                spans=[
                    FakeSpan(name="docs_index", span_type="RETRIEVER"),
                ]
            )
        )
        result = _call_scalar(
            tool_routing_accuracy,
            trace=trace,
            expectations={"expected_tools": ["docs"]},
        )
        assert result.value is True


class TestLoadSkillSpanExtraction:
    """Lazy-mode (issue #98 PR 2/2) ``_load_skill`` span handling.

    The scorer emits the ``skill_id`` argument as an additional
    "matched tool" so that ``expected_tools: ['<skill_id>']`` works
    in lazy mode the same way ``expected_tools: ['<tool_name>']``
    works in eager mode.  Existing substring matching against the
    literal ``_load_skill`` span name is preserved.
    """

    def test_skill_id_extracted_via_attribute(self) -> None:
        from bricks.evaluation.scorers.routing import tool_routing_accuracy

        trace = FakeTrace(
            data=FakeTraceData(
                spans=[
                    FakeSpan(
                        name="_load_skill",
                        span_type="TOOL",
                        attributes={"skill.loaded": "sales/quote_lookup"},
                    ),
                ]
            )
        )
        result = _call_scalar(
            tool_routing_accuracy,
            trace=trace,
            expectations={"expected_tools": ["sales/quote_lookup"]},
        )
        assert result.value is True
        assert "sales/quote_lookup" in result.rationale

    def test_skill_id_extracted_via_inputs_fallback(self) -> None:
        from bricks.evaluation.scorers.routing import tool_routing_accuracy

        # Simulate an MLflow build where attributes are stripped/empty
        # but the set_inputs payload survives.
        trace = FakeTrace(
            data=FakeTraceData(
                spans=[
                    FakeSpan(
                        name="_load_skill",
                        span_type="TOOL",
                        attributes={},
                        inputs={"skill_id": "sales/quote_lookup"},
                    ),
                ]
            )
        )
        result = _call_scalar(
            tool_routing_accuracy,
            trace=trace,
            expectations={"expected_tools": ["sales/quote_lookup"]},
        )
        assert result.value is True

    def test_substring_match_on_literal_load_skill_name(self) -> None:
        # No attribute / inputs available -- the existing substring
        # match against the literal span name must still succeed for
        # ``expected_tools: ['_load_skill']``.
        from bricks.evaluation.scorers.routing import tool_routing_accuracy

        trace = FakeTrace(
            data=FakeTraceData(
                spans=[
                    FakeSpan(name="_load_skill", span_type="TOOL"),
                ]
            )
        )
        result = _call_scalar(
            tool_routing_accuracy,
            trace=trace,
            expectations={"expected_tools": ["_load_skill"]},
        )
        assert result.value is True

    def test_attribute_takes_precedence_over_inputs(self) -> None:
        from bricks.evaluation.scorers.routing import tool_routing_accuracy

        trace = FakeTrace(
            data=FakeTraceData(
                spans=[
                    FakeSpan(
                        name="_load_skill",
                        span_type="TOOL",
                        attributes={"skill.loaded": "from_attribute"},
                        inputs={"skill_id": "from_inputs"},
                    ),
                ]
            )
        )
        result = _call_scalar(
            tool_routing_accuracy,
            trace=trace,
            expectations={"expected_tools": ["from_attribute"]},
        )
        assert result.value is True

        # And the inputs value is NOT in the matched list.
        result_negative = _call_scalar(
            tool_routing_accuracy,
            trace=trace,
            expectations={"expected_tools": ["from_inputs"]},
        )
        assert result_negative.value is False

    def test_eager_mode_unchanged_no_load_skill_span(self) -> None:
        # Regression: ordinary tool spans must continue to behave as
        # before with no skill_id extraction side-effect.
        from bricks.evaluation.scorers.routing import tool_routing_accuracy

        trace = FakeTrace(
            data=FakeTraceData(
                spans=[
                    FakeSpan(
                        name="genie_query",
                        span_type="TOOL",
                        attributes={"skill.loaded": "should_be_ignored"},
                    ),
                ]
            )
        )
        result = _call_scalar(
            tool_routing_accuracy,
            trace=trace,
            expectations={"expected_tools": ["should_be_ignored"]},
        )
        # The attribute is ignored on non-_load_skill spans; only the
        # span name participates in matching.
        assert result.value is False


# ---------------------------------------------------------------------------
# Genie SQL scorer tests
# ---------------------------------------------------------------------------


class TestGenieSqlCorrectness:
    def test_exact_match(self) -> None:
        from bricks.evaluation.scorers.genie_accuracy import genie_sql_correctness

        trace = FakeTrace(
            data=FakeTraceData(
                spans=[
                    FakeSpan(
                        name="genie_query",
                        span_type="TOOL",
                        outputs={"sql": "SELECT COUNT(*) FROM my_table"},
                    ),
                ]
            )
        )
        result = _call_scalar(
            genie_sql_correctness,
            trace=trace,
            expectations={"expected_sql": "SELECT COUNT(*) FROM my_table"},
        )
        assert result.value is True

    def test_whitespace_normalized_match(self) -> None:
        from bricks.evaluation.scorers.genie_accuracy import genie_sql_correctness

        trace = FakeTrace(
            data=FakeTraceData(
                spans=[
                    FakeSpan(
                        name="genie_query",
                        span_type="TOOL",
                        outputs={"sql": "SELECT  COUNT(*)  FROM  my_table"},
                    ),
                ]
            )
        )
        result = _call_scalar(
            genie_sql_correctness,
            trace=trace,
            expectations={"expected_sql": "SELECT COUNT(*) FROM my_table"},
        )
        assert result.value is True

    def test_mismatch(self) -> None:
        from bricks.evaluation.scorers.genie_accuracy import genie_sql_correctness

        trace = FakeTrace(
            data=FakeTraceData(
                spans=[
                    FakeSpan(
                        name="genie_query",
                        span_type="TOOL",
                        outputs={"sql": "SELECT * FROM other_table"},
                    ),
                ]
            )
        )
        result = _call_scalar(
            genie_sql_correctness,
            trace=trace,
            expectations={"expected_sql": "SELECT COUNT(*) FROM my_table"},
        )
        assert result.value is False

    def test_no_genie_span(self) -> None:
        from bricks.evaluation.scorers.genie_accuracy import genie_sql_correctness

        trace = FakeTrace(
            data=FakeTraceData(
                spans=[
                    FakeSpan(name="other_tool", span_type="TOOL", outputs={"result": "ok"}),
                ]
            )
        )
        result = _call_scalar(
            genie_sql_correctness,
            trace=trace,
            expectations={"expected_sql": "SELECT 1"},
        )
        assert result.value is False
        assert "No Genie SQL" in result.rationale

    def test_no_expectations_skips(self) -> None:
        from bricks.evaluation.scorers.genie_accuracy import genie_sql_correctness

        trace = FakeTrace()
        result = _call_scalar_or_none(genie_sql_correctness, trace=trace, expectations=None)
        assert result is None

    # ---- MCP-served Genie span discovery (issue #60) -----------------

    def test_discovers_via_tool_type_attribute(self) -> None:
        """``managed_genie`` MCP spans tagged with ``tool_type='genie'``
        are discovered regardless of span name / operator name_prefix."""
        from bricks.evaluation.scorers.genie_accuracy import genie_sql_correctness

        trace = FakeTrace(
            data=FakeTraceData(
                spans=[
                    FakeSpan(
                        name="mcp.call_tool[sales:query]",  # no 'genie' substring
                        span_type="TASK",
                        attributes={"tool_type": "genie"},
                        outputs="SQL: SELECT COUNT(*) FROM my_table\n\n<result table>",
                    ),
                ]
            )
        )
        result = _call_scalar(
            genie_sql_correctness,
            trace=trace,
            expectations={"expected_sql": "SELECT COUNT(*) FROM my_table"},
        )
        assert result.value is True

    def test_discovers_via_upstream_tool_name(self) -> None:
        """Operator name_prefix may not contain 'genie' but the upstream
        MCP tool name shape (``query_space*``) is fixed by the
        ``managed_genie`` contract."""
        from bricks.evaluation.scorers.genie_accuracy import genie_sql_correctness

        trace = FakeTrace(
            data=FakeTraceData(
                spans=[
                    FakeSpan(
                        name="mcp.call_tool[sales:query]",
                        span_type="TASK",
                        attributes={"upstream_tool_name": "query_space_01abc"},
                        outputs="SQL: SELECT 1\n\nfoo",
                    ),
                ]
            )
        )
        result = _call_scalar(
            genie_sql_correctness,
            trace=trace,
            expectations={"expected_sql": "SELECT 1"},
        )
        assert result.value is True

    def test_legacy_substring_match_still_works(self) -> None:
        """Pre-fix traces (no attributes) still score via the span-name
        substring branch."""
        from bricks.evaluation.scorers.genie_accuracy import genie_sql_correctness

        trace = FakeTrace(
            data=FakeTraceData(
                spans=[
                    FakeSpan(
                        name="ask_genie_portfolio",
                        span_type="TOOL",
                        outputs="SQL: SELECT 1\n\n<rendered table>",
                    ),
                ]
            )
        )
        result = _call_scalar(
            genie_sql_correctness,
            trace=trace,
            expectations={"expected_sql": "SELECT 1"},
        )
        assert result.value is True

    def test_sql_prefix_regex_strips_result_table(self) -> None:
        """Response payloads carry ``SQL: <stmt>`` followed by a blank
        line and the rendered result table; the regex must capture only
        the SQL statement so the equality comparison succeeds."""
        from bricks.evaluation.scorers.genie_accuracy import genie_sql_correctness

        payload = (
            "Here are the results.\n\n"
            "SQL: SELECT status, COUNT(*) FROM tbl GROUP BY status\n\n"
            "status | count\n"
            "------ | -----\n"
            "open   | 12"
        )
        trace = FakeTrace(
            data=FakeTraceData(
                spans=[
                    FakeSpan(
                        name="mcp.call_tool[g:q]",
                        span_type="TASK",
                        attributes={"tool_type": "genie"},
                        outputs=payload,
                    ),
                ]
            )
        )
        result = _call_scalar(
            genie_sql_correctness,
            trace=trace,
            expectations={
                "expected_sql": "SELECT status, COUNT(*) FROM tbl GROUP BY status",
            },
        )
        assert result.value is True


# ---------------------------------------------------------------------------
# Response quality scorer tests
# ---------------------------------------------------------------------------


class TestResponseQuality:
    def test_good_response(self) -> None:
        from bricks.evaluation.scorers.response_quality import response_quality

        long_text = "This is a detailed response " * 5 + "based on the source table data."
        trace = FakeTrace(
            data=FakeTraceData(
                spans=[
                    FakeSpan(name="root", start_time_ns=0, end_time_ns=2_000_000_000),
                ]
            )
        )
        results = _call_list(
            response_quality,
            inputs={"question": "test"},
            outputs={"response": long_text},
            trace=trace,
        )
        assert isinstance(results, list)
        assert len(results) == 3
        names = {r.name for r in results}
        assert names == {"response_length", "has_sources", "latency_ok"}

        length_fb = next(r for r in results if r.name == "response_length")
        assert length_fb.value is True

        sources_fb = next(r for r in results if r.name == "has_sources")
        assert sources_fb.value is True

        latency_fb = next(r for r in results if r.name == "latency_ok")
        assert latency_fb.value is True

    def test_short_response(self) -> None:
        from bricks.evaluation.scorers.response_quality import response_quality

        results = _call_list(
            response_quality,
            inputs={"question": "test"},
            outputs={"response": "Short answer."},
            trace=None,
        )
        length_fb = next(r for r in results if r.name == "response_length")
        assert length_fb.value is False

    def test_no_sources(self) -> None:
        from bricks.evaluation.scorers.response_quality import response_quality

        long_text = "This is a response without any references " * 3
        results = _call_list(
            response_quality,
            inputs={"question": "test"},
            outputs={"response": long_text},
            trace=None,
        )
        sources_fb = next(r for r in results if r.name == "has_sources")
        assert sources_fb.value is False

    def test_high_latency(self) -> None:
        from bricks.evaluation.scorers.response_quality import response_quality

        trace = FakeTrace(
            data=FakeTraceData(
                spans=[
                    FakeSpan(name="root", start_time_ns=0, end_time_ns=60_000_000_000),
                ]
            )
        )
        results = _call_list(
            response_quality,
            inputs={"question": "test"},
            outputs={"response": "answer " * 25},
            trace=trace,
        )
        latency_fb = next(r for r in results if r.name == "latency_ok")
        assert latency_fb.value is False

    def test_none_outputs(self) -> None:
        from bricks.evaluation.scorers.response_quality import response_quality

        results = _call_list(response_quality, inputs=None, outputs=None, trace=None)
        assert len(results) == 2  # no trace = no latency check
        length_fb = next(r for r in results if r.name == "response_length")
        assert length_fb.value is False
