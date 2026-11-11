"""Shared in-memory fakes for the ``rest_mcp`` deploy tests.

:class:`~agent_foundry.testing.fakes.FakeWorkspaceClient` records
``api_client.do`` calls, backs workspace-state reads/writes, exposes the
``apps.get`` / ``config.host`` surface, and fakes
``service_principal_secrets_proxy``.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_foundry.testing.fakes import FakeWorkspaceClient

__all__ = ["FakeWorkspaceClient"]


@pytest.fixture
def fake_ws() -> Any:
    """Factory fixture: ``fake_ws(**kwargs)`` -> :class:`FakeWorkspaceClient`."""

    def _make(**kwargs: Any) -> FakeWorkspaceClient:
        return FakeWorkspaceClient(**kwargs)

    return _make
