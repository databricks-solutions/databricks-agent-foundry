"""Tests for :mod:`payloads.agent.runtime.startup_prewarm`."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from payloads.agent.runtime.startup_prewarm import prewarm_process_caches, register_startup_prewarm


@pytest.mark.asyncio
class TestPrewarmProcessCaches:
    async def test_warms_client_and_specs(self) -> None:
        ws = MagicMock()
        config: dict[str, object] = {}
        with (
            patch("payloads.agent.runtime.startup_prewarm.get_workspace_client", return_value=ws),
            patch(
                "payloads.agent.runtime.startup_prewarm.load_agent_config_and_vars",
                return_value=(config, {}),
            ) as load,
            patch("payloads.agent.runtime.startup_prewarm._cached_brick_specs") as specs,
            patch(
                "payloads.agent.runtime.startup_prewarm.ensure_data_universe",
                new=AsyncMock(),
            ) as universe,
        ):
            await prewarm_process_caches()

        load.assert_called_once_with(ws)
        specs.assert_called_once_with(ws)
        universe.assert_not_awaited()

    async def test_warms_universe_only_when_enabled(self) -> None:
        ws = MagicMock()
        config = {"DATA_UNIVERSE_ENABLED": True}
        variables = {"agent_schema": "c.s"}
        with (
            patch("payloads.agent.runtime.startup_prewarm.get_workspace_client", return_value=ws),
            patch(
                "payloads.agent.runtime.startup_prewarm.load_agent_config_and_vars",
                return_value=(config, variables),
            ),
            patch("payloads.agent.runtime.startup_prewarm._cached_brick_specs"),
            patch(
                "payloads.agent.runtime.startup_prewarm.ensure_data_universe",
                new=AsyncMock(),
            ) as universe,
        ):
            await prewarm_process_caches()

        universe.assert_awaited_once_with(ws, config, variables)

    async def test_failure_is_logged_not_raised(self, caplog: pytest.LogCaptureFixture) -> None:
        with (
            patch(
                "payloads.agent.runtime.startup_prewarm.get_workspace_client",
                side_effect=RuntimeError("no host"),
            ),
            caplog.at_level(logging.WARNING),
        ):
            await prewarm_process_caches()

        assert "prewarm failed" in caplog.text


class TestRegisterStartupPrewarm:
    def test_runs_on_app_startup(self) -> None:
        started = MagicMock()

        async def _mark() -> None:
            started()

        app = FastAPI()
        with patch(
            "payloads.agent.runtime.startup_prewarm.prewarm_process_caches",
            _mark,
        ):
            register_startup_prewarm(app)
            with TestClient(app):
                pass

        started.assert_called_once()
