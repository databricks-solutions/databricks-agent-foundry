"""Agent App brick test fixtures.

The Lakebase capability fixtures (``lb_*``) moved to
``payloads/agent/tests/unit/deploy/conftest.py`` alongside ``test_lakebase.py``
when the provisioner relocated to ``payloads/agent/deploy`` (issue #338).
"""

from __future__ import annotations

from pathlib import Path

BRICK_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = BRICK_ROOT.parent.parent
