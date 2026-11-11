"""The ``agent`` App payload (issue #338).

The bundled agent + chat-UI workload that runs inside a Databricks App.
Selected via ``payload.adapter = agent``.  This package will own the
agent's in-container runtime (agent loop, MCP/A2A attachment, chat-UI
overrides) and the deploy-time adapter that builds its
:class:`agent_foundry.deploy.app_host.AppPayloadSpec`.

The migration lands incrementally: this slice introduces the package and
relocates the payload adapter; the runtime modules follow.
"""

from __future__ import annotations
