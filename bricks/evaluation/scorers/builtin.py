"""
Built-in LLM Judge Presets
===========================
Curated collections of MLflow built-in judges for common evaluation
scenarios.  These return instantiated judge objects ready for use with
``mlflow.genai.evaluate()`` or ``.register()`` for production monitoring.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

ScorerEntry = str | Mapping[str, Any]

BUILTIN_JUDGE_NAMES: frozenset[str] = frozenset(
    {
        "Safety",
        "RelevanceToQuery",
        "RetrievalGroundedness",
        "Correctness",
        "ToolCallCorrectness",
        "Guidelines",
    }
)

_JUDGE_REGISTRY: dict[str, type] = {}


def _lazy_import(name: str) -> Any:
    """Import a built-in judge class by name, caching in the registry."""
    if name not in BUILTIN_JUDGE_NAMES:
        raise KeyError(name)
    if name not in _JUDGE_REGISTRY:
        import mlflow.genai.scorers as mlflow_scorers

        _JUDGE_REGISTRY[name] = getattr(mlflow_scorers, name)
    return _JUDGE_REGISTRY[name]


def parse_scorer_entry(entry: ScorerEntry) -> tuple[str, dict[str, Any]]:
    """Split a config entry (``"Safety"`` or ``{name, args}``) into name and args."""
    if isinstance(entry, str):
        return entry, {}
    name = entry.get("name")
    args = entry.get("args", {})
    if not isinstance(name, str) or not isinstance(args, Mapping):
        raise ValueError(f"Scorer entry must be a name or {{name: str, args: mapping}}, got {entry!r}")
    return name, dict(args)


def _build_judge(entry: ScorerEntry) -> Any:
    name, args = parse_scorer_entry(entry)
    judge_cls = _lazy_import(name)
    try:
        return judge_cls(**args)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid args for built-in judge '{name}': {exc}") from exc


def get_builtin_judges(entries: Sequence[ScorerEntry]) -> list[Any]:
    """Instantiate built-in judges from names or ``{name, args}`` entries.

    Raises ``KeyError`` for unknown judge names and ``ValueError`` for
    malformed entries or arguments the judge rejects.
    """
    return [_build_judge(entry) for entry in entries]


# Pre-defined collections (lazy -- only import when accessed).
# ToolCallCorrectness is registered above but intentionally excluded from
# the preset: its verdicts are correct, but the Databricks judge often
# returns unparseable JSON, so rows error (issue #62). Instances can still
# opt in via their own eval_config.yml.
# RetrievalGroundedness is registered above but excluded from the default
# preset: the built-in judge errors on every row when the trace has no
# RETRIEVER span (issue #61). RAG instances opt in via traces_only.
DEFAULT_JUDGES: list[str] = ["Safety", "RelevanceToQuery"]
WITH_GROUND_TRUTH: list[str] = ["Correctness"]
