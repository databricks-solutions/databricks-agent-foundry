"""Typed contracts for tool manifests in skill ``tools.yml`` files.

These Pydantic models mirror the on-disk YAML shape consumed by the skills
assembler (``payloads/agent/deploy/skills/assemble.py``) and validated by
``payloads/agent/deploy/skills/validate.py``.

Tool variants modelled here:

- ``genie``, ``vector_search``, ``uc_function`` -- typed transports the
  agent runtime knows how to invoke directly.
- ``mcp`` -- a *reference* to a tool exposed by a server registered with
  the ``bricks/mcp/`` brick. Carries only ``name`` + ``description``; the
  validator resolves the name against the union of brick-state allow-lists
  at validate-time.

The variants are modelled as a discriminated union on the ``type`` field
so downstream code can narrow via ``match`` statements or ``isinstance``
checks without manual dispatch. ``type: "mcp"`` is the discriminator
default; YAML entries that omit ``type:`` are normalised to ``type: mcp``
by the assembler / validator before reaching this contract.

Scope note
----------
These models describe the *YAML* shape, not the runtime-resolved shape. For
example, ``GenieTool.space_id`` is optional because the assembler injects it
from state at assembly time; consumers that read the assembled manifest may
treat it as required.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Shared base
# ---------------------------------------------------------------------------

# ``extra="ignore"`` matches Pydantic's default: unknown keys are silently
# dropped. This keeps the models forward-compatible while a real-YAML parity
# test (``agent_foundry/tests/unit/test_contracts_parity.py``) guards against the
# models drifting from reality.
_MODEL_CONFIG = ConfigDict(extra="ignore")


class _ToolBase(BaseModel):
    """Fields common to every tool type."""

    model_config = _MODEL_CONFIG

    name: str
    description: str


# ---------------------------------------------------------------------------
# Tool type variants
# ---------------------------------------------------------------------------


class GenieTool(_ToolBase):
    """A Genie space tool. ``space_id`` is injected by the assembler."""

    type: Literal["genie"] = "genie"
    space_id: str | None = None


class VectorSearchTool(_ToolBase):
    """A Vector Search index tool. ``index_name`` / ``endpoint_name`` are
    injected by the assembler when omitted."""

    type: Literal["vector_search"] = "vector_search"
    index_name: str | None = None
    endpoint_name: str | None = None
    columns: list[str] = []
    query_type: str = "HYBRID"
    score_threshold: float | None = None
    num_results: int = 10


class UCFunctionTool(_ToolBase):
    """A Unity Catalog function tool. ``function_name`` is the fully
    qualified ``<catalog>.<schema>.<function>`` path (or a
    ``${var.<x>}`` template resolved by the variable substituter)."""

    type: Literal["uc_function"] = "uc_function"
    function_name: str


class PythonFunctionTool(_ToolBase):
    """A plain in-process Python callable, invoked directly by the agent
    runtime (no MCP / Genie / Vector Search / UC transport).

    This is an **instance-author primitive**, not a skill-catalog or
    end-user primitive: ``callable_path`` is config authored in the
    instance repo and is never derived from chat input, end users, or a
    shared skill catalog.

    ``callable_path`` is a dotted import path to a module-level callable,
    e.g. ``"my_pkg.my_module.my_func"``, and must resolve to instance code
    under ``instances/<name>/`` (the only tree the deploy staging ships
    alongside the framework wheel).  The runtime enforces this fail-closed
    at load: a path to an installed or stdlib module, or a callable
    re-exported from one, is rejected and the tool is skipped.

    Keep the callable to in-memory compute and route I/O (SQL, network,
    UC) through an MCP server.  The load check guarantees only
    instance-authored code runs; ambient I/O inside that code is not
    blocked in-process and is part of the instance-author contract.

    A leading parameter named ``state`` receives the request-scoped
    ``RequestState`` (the only supported per-request accumulator) and is
    not exposed as an LLM argument.

    ``params_json_schema`` is the JSON Schema for the tool's arguments.
    When omitted, the runtime infers a primitives-only schema from the
    callable's signature (see
    ``payloads.agent.runtime.tools._infer_params_schema``); pass an
    explicit schema (e.g. from a Pydantic model's ``model_json_schema()``)
    for anything richer than ``str`` / ``int`` / ``float`` / ``bool`` args.

    Unlike the MCP-served tool types, this is a *direct-mode* tool that
    stays in ``SKILL_TOOLS`` and is attached by the runtime regardless of
    the ``mcp_only`` flag (it is first-party in-process code, not the
    legacy direct-mode Genie / Vector Search that flag gates).
    """

    type: Literal["python"] = "python"
    callable_path: str
    params_json_schema: dict[str, Any] | None = None


class MCPTool(_ToolBase):
    """A reference to a tool exposed by a registered MCP server.

    Carries only ``name`` + ``description``. The skills validator
    resolves the name at validate-time against the union of allow-lists
    declared in ``instances/<app>/payloads/agent/mcp_servers/<name>/server.yml``;
    unresolved names are a validation error with a "did you forget to
    register the MCP server?" hint.

    YAML entries that omit ``type:`` entirely are normalised to
    ``type: mcp`` by the assembler / validator before this model sees
    them, so authors can write the minimal shape::

        tools:
          - name: search_issues
            description: "..."
    """

    type: Literal["mcp"] = "mcp"


# ---------------------------------------------------------------------------
# Discriminated union and manifest
# ---------------------------------------------------------------------------

Tool = Annotated[
    Union[
        GenieTool,
        VectorSearchTool,
        UCFunctionTool,
        PythonFunctionTool,
        MCPTool,
    ],
    Field(discriminator="type"),
]


class ToolManifest(BaseModel):
    """Top-level shape of a ``tools.yml`` file."""

    model_config = _MODEL_CONFIG

    tools: list[Tool] = []


__all__ = [
    "GenieTool",
    "MCPTool",
    "PythonFunctionTool",
    "Tool",
    "ToolManifest",
    "UCFunctionTool",
    "VectorSearchTool",
]
