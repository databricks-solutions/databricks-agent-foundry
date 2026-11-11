"""Generate the agent payload's ``payload.yml`` JSON Schema.

Payload-owned counterpart to the framework's
``agent_foundry.schemas._generate``: it renders the typed
:class:`payloads.agent.contracts.AgentPayloadFile` to
``agent_payload.schema.json`` next to this module so it ships with the
agent payload package.  A CI drift check asserts the committed bytes
match (see ``payloads/agent/tests/unit``).

Regenerate after any change to the agent ``spec`` contract::

    python -m payloads.agent.schemas._generate
"""

from __future__ import annotations

import json
from pathlib import Path

from payloads.agent.contracts import AgentPayloadFile

SCHEMA_FILENAME = "agent_payload.schema.json"


def schemas_dir() -> Path:
    """Absolute path to the directory holding the agent payload schema."""
    return Path(__file__).resolve().parent


def render_schema() -> str:
    """Render the agent ``payload.yml`` schema to stable, diff-friendly JSON."""
    schema = AgentPayloadFile.model_json_schema(mode="validation")
    # ``sort_keys`` + trailing newline keep the on-disk bytes stable
    # across Pydantic patch releases (matches the framework bundle style).
    return json.dumps(schema, sort_keys=True, indent=2) + "\n"


def main() -> int:
    """Regenerate the on-disk schema file in place."""
    target = schemas_dir() / SCHEMA_FILENAME
    target.write_text(render_schema(), encoding="utf-8")
    print(f"Regenerated {target}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
