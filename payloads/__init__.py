"""App payloads -- code that runs *inside* a Databricks App container (issue #338).

Top-level distinction:

* ``bricks/``    -- deploy-time **infrastructure** (idempotent IaC), including
  the ``app`` brick that provisions the Databricks App *container*.
* ``payloads/``  -- the **workload** that runs inside that container, selected
  per-instance (e.g. the ``agent`` payload: agent loop + chat UI + tool
  attachments).

A payload owns both its in-container runtime and the small deploy-time
adapter that translates the payload's config into the neutral
:class:`agent_foundry.deploy.app_host.AppPayloadSpec` the generic App host
consumes.  The host never learns a payload's internals; payloads vary
freely in shape (UI vs internal-API-only, MCP/A2A on or off, ...).
"""

from __future__ import annotations
