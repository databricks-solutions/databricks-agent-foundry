"""Build the neutral ``AppPayloadSpec`` / ``AppInfraSpec`` for ``rest_mcp``.

The proxy host is deliberately lean (no agent runtime, no UI sidecar, no
Lakebase): its container just runs the uvicorn Streamable-HTTP server
(:mod:`payloads.rest_mcp.runtime.start_server`) and needs only the
``rest_mcp`` wheel extra (``mcp`` + ``uvicorn`` + ``httpx``).

The runtime resolves its instance the framework way -- ``AGENT_FOUNDRY_PROJECT_ROOT``
(CWD at boot) + ``INSTANCE_DIR`` (``instances/<name>``, matching where staging
copies the instance) -- so no payload-specific env plumbing is required.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from agent_foundry.deploy.app_host import AppInfraSpec, AppPayloadSpec

# The container entrypoint: the uvicorn Streamable-HTTP MCP host.
_COMMAND: tuple[str, ...] = ("python", "-m", "payloads.rest_mcp.runtime.start_server")

# The lean wheel extra the container installs (mcp + uvicorn + httpx).
_REQUIREMENTS_EXTRAS: tuple[str, ...] = ("rest_mcp",)

# Placeholder used when there is no on-disk instance (dry-run / hermetic);
# the App host substitutes it from the variable bag before upload.
_INSTANCE_DIR_PLACEHOLDER = "instances/${var.instance_dir_name}"


def _instance_ref(instance_dir: Path | None) -> str:
    return f"instances/{instance_dir.name}" if instance_dir is not None else _INSTANCE_DIR_PLACEHOLDER


def build_rest_mcp_payload_spec(
    instance_dir: Path | None,
    variables: Mapping[str, str],  # noqa: ARG001 (env is static; kept for adapter-seam parity)
) -> AppPayloadSpec:
    """Return the lean runtime contract for the proxy host container."""
    env: list[dict[str, str]] = [
        {"name": "AGENT_FOUNDRY_PROJECT_ROOT", "value": "."},
        {"name": "INSTANCE_DIR", "value": _instance_ref(instance_dir)},
    ]
    return AppPayloadSpec(
        command=list(_COMMAND),
        env=list(env),
        requirements_extras=_REQUIREMENTS_EXTRAS,
    )


def build_rest_mcp_infra(instance_dir: Path | None) -> AppInfraSpec:
    """Return the minimal App infrastructure for the proxy host.

    No warehouse / Lakebase / UI resources: the proxy only needs its own
    compute and outbound egress.  Secret-scope access for ``static`` auth and
    the UC connection / MCP-Service registration are the ``provision()``
    step's concern (follow-on slice).
    """
    name = instance_dir.name if instance_dir is not None else "rest_mcp"
    return AppInfraSpec(description=f"rest_mcp REST->MCP proxy ({name})")


__all__ = ["build_rest_mcp_infra", "build_rest_mcp_payload_spec"]
