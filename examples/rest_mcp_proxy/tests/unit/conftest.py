"""rest_mcp proxy instance test fixtures.

Paths are resolved relative to the instance directory so the tests stay
self-contained and run offline (no workspace).
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

INSTANCE_DIR = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def instance_dir() -> Path:
    return INSTANCE_DIR


@pytest.fixture(scope="session")
def instance_config() -> dict:
    # Per-instance config lives in foundry.toml (typed via
    # ``agent_foundry.public.FoundryInstanceConfig``).  Read the raw dict so
    # the test asserts the on-disk shape without depending on the models.
    with open(INSTANCE_DIR / "foundry.toml", "rb") as f:
        return tomllib.load(f)
