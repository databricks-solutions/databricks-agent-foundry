"""Drift + content tests for the ``rest_mcp`` payload's shipped JSON Schema.

``rest_mcp_payload.schema.json`` is a committed generated artifact.  This
mirrors ``agent_foundry/tests/unit/test_foundry_schemas.py`` for the payload-owned schema:

1. The on-disk bytes match a fresh render of ``RestMcpPayloadFile`` -- so a
   contract change without a schema regenerate fails CI loudly.
2. The file is valid JSON with the contract-sensitive bits present
   (``additionalProperties: false`` from ``extra="forbid"``; the ``in`` alias;
   the auth discriminator).
"""

from __future__ import annotations

import json

from payloads.rest_mcp.schemas._generate import (
    SCHEMA_FILENAME,
    render_schema,
    schemas_dir,
)


def test_committed_schema_matches_freshly_generated() -> None:
    """The committed bytes must match what the generator produces *now*.

    If the contract changed but the on-disk JSON wasn't regenerated, this
    fails with a fix hint::

        python -m payloads.rest_mcp.schemas._generate
        git add payloads/rest_mcp/schemas/
    """
    on_disk = (schemas_dir() / SCHEMA_FILENAME).read_text(encoding="utf-8")
    assert on_disk == render_schema(), "Schema drift.  Run `python -m payloads.rest_mcp.schemas._generate` and commit."


def test_schema_is_valid_json_object() -> None:
    parsed = json.loads((schemas_dir() / SCHEMA_FILENAME).read_text(encoding="utf-8"))
    assert parsed["title"] == "RestMcpPayloadFile"
    assert parsed.get("type") == "object"
    assert parsed["additionalProperties"] is False


def test_schema_forbids_additional_properties_on_nested_models() -> None:
    """``extra="forbid"`` must project to ``additionalProperties: false`` so
    the YAML language server rejects unknown keys at edit time.
    """
    schema = json.loads((schemas_dir() / SCHEMA_FILENAME).read_text(encoding="utf-8"))
    defs = schema["$defs"]
    for model in ("ApiSpec", "RestConfig", "ToolSpec", "ToolRequest", "ToolArg"):
        assert defs[model]["additionalProperties"] is False, model


def test_schema_exposes_in_alias_and_auth_discriminator() -> None:
    schema = json.loads((schemas_dir() / SCHEMA_FILENAME).read_text(encoding="utf-8"))
    defs = schema["$defs"]
    # ``in`` is a Python keyword aliased from ``location``; the schema must
    # expose the on-disk authoring key so editors autocomplete it.
    assert "in" in defs["ToolArg"]["properties"]
    assert "location" not in defs["ToolArg"]["properties"]
    # Auth is a discriminated union on ``mode``.
    assert "discriminator" in defs["RestConfig"]["properties"]["auth"]
