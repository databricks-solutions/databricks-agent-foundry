"""
Offline unit tests for the scorer registry: instance scorer discovery and
its use by ``resolve_scorers`` and ``resolve_prod_scorers``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bricks.evaluation.scorers.registry import build_custom_registry, discover_instance_scorers

_SCORER_SOURCE = """
from mlflow.genai.scorers import scorer


@scorer
def {name}(outputs):
    return True
"""

_FRAMEWORK_NAMES = {"tool_routing_accuracy", "genie_sql_correctness"}


def _write_scorer_file(instance_dir: Path, filename: str, source: str) -> None:
    scorers_dir = instance_dir / "evaluation" / "scorers"
    scorers_dir.mkdir(parents=True, exist_ok=True)
    (scorers_dir / filename).write_text(source, encoding="utf-8")


def _write_scorer(instance_dir: Path, filename: str, name: str) -> None:
    _write_scorer_file(instance_dir, filename, _SCORER_SOURCE.format(name=name))


class TestDiscoverInstanceScorers:
    def test_discovers_scorers_by_name(self, tmp_path: Path) -> None:
        _write_scorer(tmp_path, "channel.py", "channel_relevance")
        _write_scorer(tmp_path, "fp.py", "false_positive")

        found = discover_instance_scorers(tmp_path)

        assert set(found) == {"channel_relevance", "false_positive"}
        assert found["channel_relevance"].name == "channel_relevance"

    def test_none_instance_dir_returns_empty(self) -> None:
        assert discover_instance_scorers(None) == {}

    def test_missing_scorers_dir_returns_empty(self, tmp_path: Path) -> None:
        assert discover_instance_scorers(tmp_path) == {}

    def test_underscore_files_are_skipped(self, tmp_path: Path) -> None:
        _write_scorer(tmp_path, "_helpers.py", "hidden_scorer")
        _write_scorer(tmp_path, "visible.py", "visible_scorer")

        assert set(discover_instance_scorers(tmp_path)) == {"visible_scorer"}

    def test_duplicate_names_raise(self, tmp_path: Path) -> None:
        _write_scorer(tmp_path, "a.py", "same_name")
        _write_scorer(tmp_path, "b.py", "same_name")

        with pytest.raises(ValueError, match="Duplicate instance scorer name 'same_name'"):
            discover_instance_scorers(tmp_path)

    def test_import_error_raises_runtime_error(self, tmp_path: Path) -> None:
        _write_scorer_file(tmp_path, "broken.py", "import does_not_exist_anywhere\n")

        with pytest.raises(RuntimeError, match="broken.py"):
            discover_instance_scorers(tmp_path)


class TestBuildCustomRegistry:
    def test_framework_scorers_without_instance(self) -> None:
        assert set(build_custom_registry(None)) == _FRAMEWORK_NAMES

    def test_merges_instance_scorers(self, tmp_path: Path) -> None:
        _write_scorer(tmp_path, "channel.py", "channel_relevance")

        assert set(build_custom_registry(tmp_path)) == _FRAMEWORK_NAMES | {"channel_relevance"}

    def test_shadowing_framework_scorer_raises(self, tmp_path: Path) -> None:
        _write_scorer(tmp_path, "routing.py", "tool_routing_accuracy")

        with pytest.raises(ValueError, match="shadows a framework scorer"):
            build_custom_registry(tmp_path)

    def test_reexported_framework_scorer_is_not_a_clash(self, tmp_path: Path) -> None:
        _write_scorer_file(
            tmp_path,
            "reexport.py",
            "from bricks.evaluation.scorers.routing import tool_routing_accuracy\n",
        )

        assert set(build_custom_registry(tmp_path)) == _FRAMEWORK_NAMES


class TestResolversUseInstanceScorers:
    def test_resolve_scorers_picks_up_instance_scorer(self, tmp_path: Path) -> None:
        from bricks.evaluation.scripts.run_evaluation import resolve_scorers

        _write_scorer(tmp_path, "channel.py", "channel_relevance")
        config = {"scorers": {"custom": ["channel_relevance", "tool_routing_accuracy"]}}

        scorers = resolve_scorers(config, instance_dir=tmp_path)

        assert [s.name for s in scorers] == ["channel_relevance", "tool_routing_accuracy"]

    def test_resolve_prod_scorers_picks_up_instance_scorer(self, tmp_path: Path) -> None:
        from bricks.evaluation.scripts.register_monitors import resolve_prod_scorers

        _write_scorer(tmp_path, "channel.py", "channel_relevance")
        config = {"production_monitoring": {"scorers": ["Safety", "channel_relevance"]}}

        resolved = resolve_prod_scorers(config, tmp_path)

        assert [name for name, _ in resolved] == ["safety", "channel_relevance"]
