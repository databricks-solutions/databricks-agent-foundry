"""Generate the ``rest_mcp`` payload's ``payload.yml`` JSON Schema.

Payload-owned counterpart to the framework's
``agent_foundry.schemas._generate`` (and the sibling
``payloads.agent.schemas._generate``): it renders the typed
:class:`payloads.rest_mcp.contracts.RestMcpPayloadFile` to
``rest_mcp_payload.schema.json`` next to this module so it ships with the
``rest_mcp`` payload package.  A CI drift check asserts the committed bytes
match (see ``payloads/rest_mcp/tests/unit``).

Regenerate after any change to the ``rest_mcp`` ``spec`` contract::

    python -m payloads.rest_mcp.schemas._generate
"""

from __future__ import annotations

import json
from pathlib import Path

from payloads.rest_mcp.contracts import RestMcpPayloadFile

SCHEMA_FILENAME = "rest_mcp_payload.schema.json"


def schemas_dir() -> Path:
    """Absolute path to the directory holding the ``rest_mcp`` payload schema."""
    return Path(__file__).resolve().parent


def render_schema() -> str:
    """Render the ``rest_mcp`` ``payload.yml`` schema to stable JSON."""
    schema = RestMcpPayloadFile.model_json_schema(mode="validation")
    # ``sort_keys`` + trailing newline keep the on-disk bytes stable across
    # Pydantic patch releases (matches the framework bundle style).
    return json.dumps(schema, sort_keys=True, indent=2) + "\n"


def main() -> int:
    """Regenerate the on-disk schema file in place."""
    target = schemas_dir() / SCHEMA_FILENAME
    target.write_text(render_schema(), encoding="utf-8")
    print(f"Regenerated {target}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
