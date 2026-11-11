"""Brick scaffold completeness guard for Pydantic-backed bricks.

Each Pydantic-backed brick ships a fully-commented reference at
``bricks/<brick>/_scaffold/<file>`` that surfaces every knob the
brick's typed config accepts.  This test enforces the "reference"
half of that contract: when a brick author adds a new field to the
typed model, CI fails until the scaffold has been updated too.

Contract
--------

A field is considered "covered" by the scaffold if it appears in
the YAML as either:

* an active top-level key (parsed via PyYAML), or
* a commented-out key of the form ``# <name>:`` (a placeholder the
  author left in the file for discoverability).

Both forms count: rarely-used fields (e.g. ``tools`` on the agent)
are explicitly allowed to ship commented-out, but they MUST appear
somewhere in the file so future maintainers see them.

Scope
-----

Pydantic-backed bricks only:

* ``warehouse``  -> ``agent_foundry.config.WarehouseConfig``
* ``evaluation`` -> ``agent_foundry.config.EvaluationConfig``

The agent payload's authoring surface moved out of the framework into a
payload-owned ``spec`` (``payloads.agent.contracts.AgentPayloadSpec``);
its ``payload.yml`` scaffold is covered by a dedicated nested check
below rather than the flat top-level mapping used for bricks.

Directory-shaped bricks (``genie``, ``vector_search``, ``mcp``,
``skills``, ``data``, ``sql_function``) have a multi-file, partly-
conventional contract and are out of scope for this test -- they
will get a brick-shape-specific completeness check in a follow-up.
``agent_app`` is now Pydantic-backed too
(:class:`agent_foundry.contracts.agent_app.AgentAppConfig`): the
merged config is validated at deploy time with ``extra="forbid"``,
so its scaffold is covered by this test like the other typed bricks.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import BaseModel

from agent_foundry.config import EvaluationConfig, WarehouseConfig
from agent_foundry.contracts.agent_app import AgentAppConfig
from agent_foundry.contracts.jobs import JobConfig
from payloads.agent.contracts import AgentPayloadSpec
from payloads.rest_mcp.contracts import RestMcpPayloadSpec

# Repo root -- ``tests/`` lives directly under it, so one ``parent``
# call is enough.  This intentionally matches the convention used by
# the rest of ``agent_foundry.scaffold._BRICKS_DIR``.
_REPO_ROOT: Path = Path(__file__).resolve().parents[2]

# Map: scaffold file path -> Pydantic model whose top-level fields
# the scaffold must cover.  Add a row here when a new Pydantic-backed
# brick gets a scaffold; the test will pick it up automatically.
_PYDANTIC_SCAFFOLDS: dict[str, type[BaseModel]] = {
    "bricks/warehouse/_scaffold/config.yml": WarehouseConfig,
    "bricks/app/_scaffold/config.yml": AgentAppConfig,
    "bricks/evaluation/_scaffold/eval_config.yml": EvaluationConfig,
    "bricks/job/_scaffold/job.yml": JobConfig,
}

# Match commented-out top-level YAML keys: a ``#`` at the start of
# the line, optional whitespace, then ``<key>:``.  Keys are conservative
# (lowercase letters, digits, underscores) to match Pydantic field
# naming conventions and avoid false positives on prose comments.
_COMMENTED_KEY_RE = re.compile(r"^#\s*([a-z_][a-z0-9_]*)\s*:", re.MULTILINE)


def _active_keys(yaml_text: str) -> set[str]:
    """Return the set of top-level keys actually parsed by PyYAML."""
    data: Any = yaml.safe_load(yaml_text)
    if data is None:
        return set()
    if not isinstance(data, dict):
        # An eval_config.yml that parses to a list is a real bug --
        # surface it loudly rather than swallowing it as "no keys".
        raise AssertionError(f"Scaffold YAML must parse to a mapping at top level, got {type(data).__name__}")
    return set(data.keys())


def _commented_keys(yaml_text: str) -> set[str]:
    """Return the set of top-level keys that appear commented out."""
    return set(_COMMENTED_KEY_RE.findall(yaml_text))


@pytest.mark.parametrize(
    "scaffold_rel_path,model_cls",
    sorted(_PYDANTIC_SCAFFOLDS.items()),
)
def test_pydantic_scaffold_covers_every_model_field(
    scaffold_rel_path: str,
    model_cls: type[BaseModel],
) -> None:
    """Every typed field in *model_cls* must appear in the scaffold,
    either as an active key or as a commented-out placeholder.

    Failure message points the brick author at the exact fix: add
    the missing field to ``bricks/<x>/_scaffold/<file>`` with a
    comment explaining what it controls.
    """
    scaffold_path = _REPO_ROOT / scaffold_rel_path
    assert scaffold_path.is_file(), f"scaffold file missing: {scaffold_path}"

    yaml_text = scaffold_path.read_text(encoding="utf-8")
    covered = _active_keys(yaml_text) | _commented_keys(yaml_text)
    expected = set(model_cls.model_fields)

    missing = expected - covered
    assert not missing, (
        f"{scaffold_rel_path} is missing fields from {model_cls.__name__}: "
        f"{sorted(missing)}.  Add each as either an active YAML key with "
        "the framework default, or a commented-out '# field:' placeholder "
        "with a one-line comment explaining what it controls."
    )


# ---------------------------------------------------------------------------
# Agent payload scaffold (payload.yml) -- nested ``spec`` completeness
# ---------------------------------------------------------------------------
#
# The agent payload's authoring surface is the ``spec`` block of the
# ``payload.yml`` scaffold, not a flat top-level model.  This guard
# asserts that every section of the typed ``AgentPayloadSpec``
# (runtime / ui / integrations) is represented under ``payload.spec`` so
# adding a new top-level spec section forces a scaffold update.

_AGENT_PAYLOAD_SCAFFOLD = "payloads/agent/_scaffold/payload.yml"


def test_agent_payload_scaffold_covers_every_spec_section() -> None:
    scaffold_path = _REPO_ROOT / _AGENT_PAYLOAD_SCAFFOLD
    assert scaffold_path.is_file(), f"scaffold file missing: {scaffold_path}"

    data = yaml.safe_load(scaffold_path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), "payload.yml scaffold must parse to a mapping"
    spec = data.get("payload", {}).get("spec", {})
    assert isinstance(spec, dict), "payload.spec must be a mapping in the scaffold"

    expected = set(AgentPayloadSpec.model_fields)
    missing = expected - set(spec.keys())
    assert not missing, (
        f"{_AGENT_PAYLOAD_SCAFFOLD} payload.spec is missing sections from "
        f"AgentPayloadSpec: {sorted(missing)}.  Add each section under "
        "payload.spec with commented examples of its toggles."
    )


# Match a commented-out key at any indentation, for nested placeholders
# such as ``        # max_mb: 10``.  The top-level ``_COMMENTED_KEY_RE``
# deliberately anchors ``#`` to column 0 and would miss those.
_NESTED_COMMENTED_KEY_RE = re.compile(r"^\s*#\s*([a-z_][a-z0-9_]*)\s*:", re.MULTILINE)


def _section_models(spec_cls: type[BaseModel]) -> list[tuple[str, type[BaseModel]]]:
    """Return ``(field name, model)`` for each sub-model section of *spec_cls*.

    Derived from the model rather than hand-listed so a new spec section
    is picked up by the guard automatically.
    """
    sections: list[tuple[str, type[BaseModel]]] = []
    for name, field in spec_cls.model_fields.items():
        annotation = field.annotation
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            sections.append((name, annotation))
    return sections


_AGENT_SPEC_SECTIONS = _section_models(AgentPayloadSpec)


@pytest.mark.parametrize(
    ("section", "model_cls"),
    _AGENT_SPEC_SECTIONS,
    ids=[name for name, _ in _AGENT_SPEC_SECTIONS],
)
def test_agent_payload_scaffold_documents_every_section_field(
    section: str,
    model_cls: type[BaseModel],
) -> None:
    """Every knob one level inside a spec section must be discoverable.

    The section-level guard above only sees ``runtime`` / ``ui`` /
    ``integrations``, so a new toggle nested under one of them (say
    ``spec.ui.tool_output``) could ship with no scaffold entry and no
    failing test.  The scaffold is the reference an author reads before
    writing a payload, so an undocumented knob is effectively invisible.
    """
    scaffold_path = _REPO_ROOT / _AGENT_PAYLOAD_SCAFFOLD
    yaml_text = scaffold_path.read_text(encoding="utf-8")
    data = yaml.safe_load(yaml_text)
    authored = data.get("payload", {}).get("spec", {}).get(section) or {}
    assert isinstance(authored, dict), f"payload.spec.{section} must be a mapping in the scaffold"

    covered = set(authored) | set(_NESTED_COMMENTED_KEY_RE.findall(yaml_text))
    missing = set(model_cls.model_fields) - covered
    assert not missing, (
        f"{_AGENT_PAYLOAD_SCAFFOLD} payload.spec.{section} is missing fields "
        f"from {model_cls.__name__}: {sorted(missing)}.  Add each as an active "
        "YAML key with the framework default, or as a commented-out "
        "'# field:' placeholder with a one-line comment explaining it."
    )


_REST_MCP_PAYLOAD_SCAFFOLD = "payloads/rest_mcp/_scaffold/payload.yml"


def test_rest_mcp_payload_scaffold_covers_every_spec_section() -> None:
    scaffold_path = _REPO_ROOT / _REST_MCP_PAYLOAD_SCAFFOLD
    assert scaffold_path.is_file(), f"scaffold file missing: {scaffold_path}"

    data = yaml.safe_load(scaffold_path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), "payload.yml scaffold must parse to a mapping"
    spec = data.get("payload", {}).get("spec", {})
    assert isinstance(spec, dict), "payload.spec must be a mapping in the scaffold"

    missing = set(RestMcpPayloadSpec.model_fields) - set(spec.keys())
    assert not missing, (
        f"{_REST_MCP_PAYLOAD_SCAFFOLD} payload.spec is missing sections from "
        f"RestMcpPayloadSpec: {sorted(missing)}. Add each section under "
        "payload.spec with commented examples of its toggles."
    )
