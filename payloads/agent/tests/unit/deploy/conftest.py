"""Test fixtures for the agent payload's deploy-time glue.

The Lakebase capability fixtures were relocated here together with
``test_lakebase.py`` (issue #338) when the provisioner moved from
``bricks/app/scripts/lakebase.py`` into ``payloads/agent/deploy/``.
Names keep the ``lb_`` prefix so the test module reads as the moved
original.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def lb_instance_dir(tmp_path: Path) -> Path:
    """Create a minimal instance directory named ``test-app``."""
    inst = tmp_path / "instances" / "test-app"
    inst.mkdir(parents=True)
    return inst


@pytest.fixture
def lb_variables() -> dict[str, str]:
    """Standard variables for Lakebase provisioning offline tests."""
    return {
        "target_name": "dev",
        "app_name": "test-app",
        "full_app_name": "dev-test-app-app",
    }


@pytest.fixture
def lb_state_with_sp() -> dict[str, str]:
    """Previous Lakebase state that includes an SP client ID."""
    state = {
        "lakebase_project_name": "dev-test-app-lakebase",
        "lakebase_branch_path": "projects/dev-test-app-lakebase/branches/production",
        "lakebase_database_path": ("projects/dev-test-app-lakebase/branches/production/databases/db-old-123"),
        "sp_client_id": "aaaa-old-sp-id",
    }
    return state
