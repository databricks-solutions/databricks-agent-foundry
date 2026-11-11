"""
Built-in LLM Judge Presets
===========================
Curated collections of MLflow built-in judges for common evaluation
scenarios.  These return instantiated judge objects ready for use with
``mlflow.genai.evaluate()`` or ``.register()`` for production monitoring.
"""

from __future__ import annotations

from typing import Any

_JUDGE_REGISTRY: dict[str, type] = {}


def _lazy_import(name: str) -> Any:
    """Import a built-in judge class by name, caching in the registry."""
    if name not in _JUDGE_REGISTRY:
        from mlflow.genai.scorers import (
            Correctness,
            Guidelines,
            RelevanceToQuery,
            RetrievalGroundedness,
            Safety,
            ToolCallCorrectness,
        )

        _JUDGE_REGISTRY.update(
            {
                "Safety": Safety,
                "RelevanceToQuery": RelevanceToQuery,
                "RetrievalGroundedness": RetrievalGroundedness,
                "Correctness": Correctness,
                "ToolCallCorrectness": ToolCallCorrectness,
                "Guidelines": Guidelines,
            }
        )
    return _JUDGE_REGISTRY[name]


def get_builtin_judges(names: list[str]) -> list[Any]:
    """Instantiate built-in judges by name.

    Raises ``KeyError`` for unknown judge names.
    """
    return [_lazy_import(name)() for name in names]


# Pre-defined collections (lazy -- only import when accessed).
# ToolCallCorrectness is registered above but intentionally excluded from
# the preset: MLflow 3.11.x tool-extractor fails pydantic validation on
# every trace (issue #48). Instances can still opt in via their own
# eval_config.yml.
# RetrievalGroundedness is registered above but excluded from the default
# preset: the built-in judge errors on every row when the trace has no
# RETRIEVER span (issue #61). RAG instances opt in via traces_only.
DEFAULT_JUDGES: list[str] = ["Safety", "RelevanceToQuery"]
WITH_GROUND_TRUTH: list[str] = ["Correctness"]
