"""SQL function brick test fixtures."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

BRICK_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DIR = BRICK_ROOT / "_scaffold"


@pytest.fixture
def fn_instance_dir(tmp_path: Path) -> Path:
    """Copy the _scaffold fixture to a temp directory for isolated testing."""
    dest = tmp_path / "instances" / "sql_functions" / "fn_smoke"
    shutil.copytree(FIXTURE_DIR, dest)
    return dest


@pytest.fixture
def fn_variables() -> dict[str, str]:
    """Standard variables for SQL function brick offline tests."""
    return {
        "catalog": "test_catalog",
        "schema_prefix": "test_app",
        "agent_schema": "test_catalog.test_app_agent",
        "agent_schema_name": "test_app_agent",
        "warehouse_id": "test_warehouse_123",
        "app_name": "test-app",
    }
