"""
Offline unit tests for ``{name, args}`` scorer entries: configurable
built-in judges in eval and production monitoring.
"""

from __future__ import annotations

from typing import Any

import pytest

from bricks.evaluation.scorers.builtin import get_builtin_judges, parse_scorer_entry

_GROUNDING: dict[str, Any] = {
    "name": "Guidelines",
    "args": {"name": "grounding", "guidelines": "Every factual claim is supported by tool outputs."},
}


class TestParseScorerEntry:
    def test_plain_name(self) -> None:
        assert parse_scorer_entry("Safety") == ("Safety", {})

    def test_mapping_with_args(self) -> None:
        assert parse_scorer_entry(_GROUNDING) == ("Guidelines", _GROUNDING["args"])

    def test_mapping_without_args(self) -> None:
        assert parse_scorer_entry({"name": "Safety"}) == ("Safety", {})

    @pytest.mark.parametrize("entry", [{"args": {}}, {"name": "Guidelines", "args": "text"}])
    def test_malformed_entry_raises(self, entry: dict[str, Any]) -> None:
        with pytest.raises(ValueError, match="Scorer entry must be"):
            parse_scorer_entry(entry)


class TestGetBuiltinJudges:
    def test_mixes_plain_and_configured_entries(self) -> None:
        safety, grounding = get_builtin_judges(["Safety", _GROUNDING])

        assert safety.name == "safety"
        assert grounding.name == "grounding"
        assert grounding.guidelines == _GROUNDING["args"]["guidelines"]

    def test_missing_required_arg_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="Invalid args for built-in judge 'Guidelines'"):
            get_builtin_judges(["Guidelines"])

    def test_unknown_judge_raises_key_error(self) -> None:
        with pytest.raises(KeyError):
            get_builtin_judges(["NotAJudge"])


class TestResolversAcceptEntries:
    def test_resolve_scorers_builds_configured_judge(self) -> None:
        from bricks.evaluation.scripts.run_evaluation import resolve_scorers

        scorers = resolve_scorers({"scorers": {"default": ["Safety", _GROUNDING]}})

        assert [s.name for s in scorers] == ["safety", "grounding"]

    def test_resolve_scorers_rejects_args_on_custom_scorer(self) -> None:
        from bricks.evaluation.scripts.run_evaluation import resolve_scorers

        config = {"scorers": {"custom": [{"name": "tool_routing_accuracy", "args": {"x": 1}}]}}

        with pytest.raises(ValueError, match="does not take args"):
            resolve_scorers(config)

    def test_prod_registration_names(self) -> None:
        from bricks.evaluation.scripts.register_monitors import resolve_prod_scorers

        config = {"production_monitoring": {"scorers": ["RelevanceToQuery", _GROUNDING, "tool_routing_accuracy"]}}

        names = [name for name, _ in resolve_prod_scorers(config)]

        assert names == ["relevancetoquery", "grounding", "tool_routing_accuracy"]

    def test_prod_rejects_args_on_custom_scorer(self) -> None:
        from bricks.evaluation.scripts.register_monitors import resolve_prod_scorers

        config = {"production_monitoring": {"scorers": [{"name": "tool_routing_accuracy", "args": {"x": 1}}]}}

        with pytest.raises(ValueError, match="does not take args"):
            resolve_prod_scorers(config)
