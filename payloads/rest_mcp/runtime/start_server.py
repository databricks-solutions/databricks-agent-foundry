"""Process entrypoint for the ``rest_mcp`` Streamable-HTTP host.

Resolves the active instance (``INSTANCE_DIR``), loads + validates its spec,
builds the multi-API ASGI app, and serves it with uvicorn on port 8000 -- the
port the generic Databricks App host proxies.  The deploy adapter wires this as
the App ``command`` and sets the env contract; it is not normally run by hand.

Usage::

    INSTANCE_DIR=instances/<name> python -m payloads.rest_mcp.runtime.start_server
"""

from __future__ import annotations

import argparse
from pathlib import Path

from agent_foundry import env
from payloads.rest_mcp.runtime.config import load_rest_mcp_spec
from payloads.rest_mcp.runtime.server.app import build_app


def _resolve_instance_dir() -> Path:
    """Resolve ``INSTANCE_DIR`` (absolute, or relative to the project root)."""
    instance = Path(env.read(env.INSTANCE_DIR))
    if instance.is_absolute():
        return instance
    project_root = env.read_optional(env.AGENT_FOUNDRY_PROJECT_ROOT) or "."
    return Path(project_root) / instance


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Start the rest_mcp Streamable-HTTP MCP host")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8000, help="Bind port (default: 8000)")
    args = parser.parse_args(argv)

    instance_dir = _resolve_instance_dir()
    spec = load_rest_mcp_spec(instance_dir)
    app = build_app(spec, instance_dir)

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
