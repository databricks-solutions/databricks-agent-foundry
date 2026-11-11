"""Guaranteed teardown helper for ephemeral upstream deploys."""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


def run_then_destroy(action: Callable[[], T], destroy: Callable[[], None]) -> T:
    """Run *action* and always invoke *destroy*, even on failure."""
    try:
        return action()
    finally:
        destroy()
