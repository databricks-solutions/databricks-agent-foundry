"""
Evaluation Scorer Catalog
==========================
Reusable scorer functions for both development evaluation
(``mlflow.genai.evaluate()``) and production monitoring
(``scorer.register().start()``).

All custom scorers use the ``@scorer`` decorator for production
monitoring compatibility.
"""

from __future__ import annotations

from bricks.evaluation.scorers.builtin import (
    BUILTIN_JUDGE_NAMES,
    DEFAULT_JUDGES,
    WITH_GROUND_TRUTH,
    get_builtin_judges,
)
from bricks.evaluation.scorers.genie_accuracy import genie_sql_correctness
from bricks.evaluation.scorers.routing import tool_routing_accuracy

CUSTOM_SCORERS = [
    tool_routing_accuracy,
    genie_sql_correctness,
]

__all__ = [
    "BUILTIN_JUDGE_NAMES",
    "DEFAULT_JUDGES",
    "WITH_GROUND_TRUTH",
    "CUSTOM_SCORERS",
    "get_builtin_judges",
    "tool_routing_accuracy",
    "genie_sql_correctness",
]
