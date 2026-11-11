"""After destroy, every data domain schema is dropped."""

from __future__ import annotations

from typing import Any

import pytest
from databricks.sdk.errors import NotFound

pytestmark = pytest.mark.gone


def test_domain_schemas_are_dropped(live_ws: Any, gone_domain_schemas: list[str]) -> None:
    remaining = []
    for qualified in gone_domain_schemas:
        try:
            live_ws.schemas.get(qualified)
        except NotFound:
            continue
        remaining.append(qualified)
    assert not remaining, f"schemas still exist after destroy: {remaining}"
