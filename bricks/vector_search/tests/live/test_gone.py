"""After destroy, every published index is deleted (the shared endpoint stays)."""

from __future__ import annotations

from typing import Any

import pytest
from databricks.sdk.errors import NotFound

pytestmark = pytest.mark.gone


def _exists(ws: Any, index_name: str) -> bool:
    try:
        ws.vector_search_indexes.get_index(index_name)
    except NotFound:
        return False
    return True


def test_indexes_are_deleted(live_ws: Any, gone_index_names: dict[str, str]) -> None:
    remaining = {name: idx for name, idx in gone_index_names.items() if _exists(live_ws, idx)}
    assert not remaining, f"vector indexes still exist after destroy: {remaining}"
