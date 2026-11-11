"""Data brick test fixtures."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

BRICK_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = BRICK_ROOT.parent.parent
FIXTURE_DIR = BRICK_ROOT / "_scaffold"


@pytest.fixture
def data_instance_dir(tmp_path: Path) -> Path:
    """Copy the data_smoke fixture to a temp directory for isolated testing."""
    dest = tmp_path / "instances" / "data_domains" / "data_smoke"
    shutil.copytree(FIXTURE_DIR, dest)
    return dest


@pytest.fixture
def data_variables() -> dict[str, str]:
    """Standard variables for data brick offline tests."""
    return {
        "catalog": "test_catalog",
        "schema": "unused_global_schema",
        "schema_prefix": "test_pfx",
        "warehouse_id": "test_warehouse_123",
    }
