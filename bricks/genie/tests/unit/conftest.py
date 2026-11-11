"""Genie brick test fixtures."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from bricks.genie.scripts.assemble import assemble_api_payload, assemble_serialized_space

BRICK_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = BRICK_ROOT.parent.parent
FIXTURE_DIR = BRICK_ROOT / "_scaffold"


@pytest.fixture
def smoke_instance_dir(tmp_path: Path) -> Path:
    """Copy the genie_smoke fixture to a temp directory for isolated testing."""
    dest = tmp_path / "instances" / "genie_spaces" / "genie_smoke"
    shutil.copytree(FIXTURE_DIR, dest)

    (tmp_path / "pyproject.toml").write_text('[project]\nname = "test"\n')

    return dest


@pytest.fixture
def assembled_space(smoke_instance_dir: Path) -> dict[str, Any]:
    """Assemble the smoke fixture into a serialized_space dict."""
    return assemble_serialized_space(smoke_instance_dir, seed_prefix="genie_smoke")


@pytest.fixture
def assembled_payload(smoke_instance_dir: Path) -> dict[str, Any]:
    """Assemble the smoke fixture into a full API payload."""
    return assemble_api_payload(smoke_instance_dir)
