"""After destroy, the scaffold's UC function is dropped."""

from __future__ import annotations

from typing import Any

import pytest
from databricks.sdk.errors import NotFound

pytestmark = pytest.mark.gone


def test_scaffold_function_is_dropped(live_ws: Any, gone_scaffold_function: tuple[str, str, str]) -> None:
    full_name = ".".join(gone_scaffold_function)
    with pytest.raises(NotFound):
        live_ws.functions.get(full_name)
