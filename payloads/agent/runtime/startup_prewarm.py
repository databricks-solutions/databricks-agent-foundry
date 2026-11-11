"""Fill identity-independent process caches before the first request.

The first request after a process start currently blocks on a SQL
warehouse read for the data-universe prompt block (several seconds when
the warehouse is cold) and a workspace-state read for brick MCP specs
(~0.5s).  Both are independent of the caller, so they can run as soon as
the app is up.

The prewarm is fire-and-forget off the FastAPI lifespan: if a request
arrives first, the existing caches and locks still serialize the work.
Failures are logged and never prevent the app from serving.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI

from payloads.agent.runtime.brick_mcp_servers import _cached_brick_specs
from payloads.agent.runtime.data_universe import ensure_data_universe
from payloads.agent.runtime.request_context import get_workspace_client
from payloads.agent.runtime.runtime_config import load_agent_config_and_vars

logger = logging.getLogger(__name__)


async def prewarm_process_caches() -> None:
    """Populate the SP client, brick-spec cache, and data-universe block.

    Never raises.  Brick specs are read without I/O when the instance has
    no ``mcp_servers/`` tree.
    """
    try:
        ws = get_workspace_client()
        config, variables = load_agent_config_and_vars(ws)
        _cached_brick_specs(ws)
        if config.get("DATA_UNIVERSE_ENABLED"):
            await ensure_data_universe(ws, config, variables)
        logger.info("[startup] process cache prewarm finished")
    except Exception:
        logger.warning("[startup] process cache prewarm failed", exc_info=True)


def register_startup_prewarm(app: FastAPI) -> None:
    """Schedule :func:`prewarm_process_caches` on the app lifespan.

    Wrapped around any existing lifespan so MCP/A2A route registration
    stays in charge of its own startup.  The task is cancelled on
    shutdown if it is still running.
    """
    original_lifespan = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def _combined_lifespan(app_instance: FastAPI) -> AsyncIterator[dict[str, Any]]:
        task = asyncio.create_task(prewarm_process_caches())
        try:
            if original_lifespan is not None:
                async with original_lifespan(app_instance) as state:
                    yield dict(state) if state else {}
            else:
                yield {}
        finally:
            if not task.done():
                task.cancel()
            await asyncio.wait({task})

    app.router.lifespan_context = _combined_lifespan
    logger.info("[startup] process cache prewarm registered")
