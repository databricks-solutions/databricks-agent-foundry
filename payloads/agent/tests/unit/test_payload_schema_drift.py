"""The shipped ``agent_payload.schema.json`` matches the ``spec`` contract."""

from __future__ import annotations

from payloads.agent.schemas._generate import SCHEMA_FILENAME, render_schema, schemas_dir


def test_committed_schema_matches_contract() -> None:
    committed = (schemas_dir() / SCHEMA_FILENAME).read_text(encoding="utf-8")
    assert committed == render_schema(), "stale schema: run `python -m payloads.agent.schemas._generate`"
