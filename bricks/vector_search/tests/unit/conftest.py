"""Vector Search brick test fixtures."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

BRICK_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = BRICK_ROOT.parent.parent
FIXTURE_DIR = BRICK_ROOT / "_scaffold"


@pytest.fixture
def vs_instance_dir(tmp_path: Path) -> Path:
    """Copy the vs_smoke fixture to a temp directory for isolated testing."""
    dest = tmp_path / "instances" / "demo" / "vector_indexes" / "vs_smoke"
    shutil.copytree(FIXTURE_DIR, dest)
    return dest


@pytest.fixture
def vs_variables() -> dict[str, str]:
    """Standard variables for Vector Search brick offline tests."""
    return {
        "catalog": "test_catalog",
        "schema": "test_schema",
        "vs_endpoint_name": "test-vs-endpoint",
        "index_name": "test_docs_index",
        "source_table": "test_docs",
    }
