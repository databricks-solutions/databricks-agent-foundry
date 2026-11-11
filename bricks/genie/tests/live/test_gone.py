"""After destroy, every Genie space the canary published is trashed."""

from __future__ import annotations

from typing import Any

import pytest
from databricks.sdk.errors import NotFound
from databricks.sdk.errors.base import DatabricksError

pytestmark = pytest.mark.gone


def _is_gone(ws: Any, space_id: str) -> bool:
    try:
        ws.genie.get_space(space_id)
    except NotFound:
        return True
    except DatabricksError as exc:
        return "trash" in str(exc).lower()
    return False


def test_spaces_are_trashed(live_ws: Any, gone_space_ids: dict[str, str]) -> None:
    remaining = {name: sid for name, sid in gone_space_ids.items() if not _is_gone(live_ws, sid)}
    assert not remaining, f"Genie spaces still live after destroy: {remaining}"
