"""Runtime config loader for the ``rest_mcp`` payload (payload-owned).

The framework hands the payload an opaque ``spec`` mapping (the ``payload.spec``
block of ``instances/<name>/payloads/rest_mcp/payload.yml``).  This module is
the payload's typed view of it, mirroring :mod:`payloads.agent.config`:
:func:`load_rest_mcp_spec` reads + validates that block into the typed
:class:`~payloads.rest_mcp.contracts.RestMcpPayloadSpec`.

Unlike the agent payload there is no meaningful all-defaults fallback -- a
``rest_mcp`` instance is defined by the APIs it declares -- so an absent or
empty spec fails validation (``apis`` is required), which is the correct
fail-closed behaviour for a proxy with nothing to proxy.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agent_foundry._payload_spec import read_spec
from payloads.rest_mcp.contracts import RestMcpPayloadSpec

PAYLOAD_NAME = "rest_mcp"


def load_rest_mcp_spec(instance_dir: Path, *, payload: str = PAYLOAD_NAME) -> RestMcpPayloadSpec:
    """Read and validate the ``rest_mcp`` payload spec from ``payload.yml``.

    Raises ``pydantic.ValidationError`` when the spec is absent or malformed --
    a proxy instance must declare at least one API.
    """
    return RestMcpPayloadSpec.model_validate(read_spec(instance_dir, payload=payload))


def resolve_rest_mcp_spec(
    instance_dir: Path | None,
    raw_spec: Mapping[str, Any] | None = None,
    *,
    payload: str = PAYLOAD_NAME,
) -> RestMcpPayloadSpec:
    """Resolve the typed spec, preferring the on-disk ``payload.yml``.

    Mirrors :func:`payloads.agent.config.resolve_agent_spec`: when
    *instance_dir* is given it is the source of truth; otherwise the
    *raw_spec* mapping the framework passed through the adapter seam is
    validated (dry-run / hermetic paths that have no instance directory).
    """
    if instance_dir is not None:
        return load_rest_mcp_spec(instance_dir, payload=payload)
    return RestMcpPayloadSpec.model_validate(dict(raw_spec or {}))


__all__ = ["PAYLOAD_NAME", "load_rest_mcp_spec", "resolve_rest_mcp_spec"]
