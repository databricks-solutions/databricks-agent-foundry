"""Unit tests for ``agent_foundry.contracts.tools``."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent_foundry.contracts.tools import (
    GenieTool,
    MCPTool,
    PythonFunctionTool,
    ToolManifest,
    UCFunctionTool,
    VectorSearchTool,
)


class TestGenieTool:
    def test_minimal(self) -> None:
        t = GenieTool(name="ask", description="query X")
        assert t.type == "genie"
        assert t.space_id is None

    def test_with_space_id(self) -> None:
        t = GenieTool(name="ask", description="d", space_id="01eff0")
        assert t.space_id == "01eff0"

    def test_missing_name_raises(self) -> None:
        with pytest.raises(ValidationError):
            GenieTool(description="d")  # type: ignore[call-arg]


class TestVectorSearchTool:
    def test_minimal(self) -> None:
        t = VectorSearchTool(name="docs", description="d")
        assert t.type == "vector_search"
        assert t.query_type == "HYBRID"
        assert t.num_results == 10

    def test_full(self) -> None:
        t = VectorSearchTool(
            name="docs",
            description="d",
            index_name="cat.sch.idx",
            endpoint_name="ep",
            columns=["id", "content"],
            query_type="ANN",
            score_threshold=0.8,
            num_results=5,
        )
        assert t.columns == ["id", "content"]
        assert t.score_threshold == 0.8


class TestUCFunctionTool:
    def test_requires_function_name(self) -> None:
        with pytest.raises(ValidationError):
            UCFunctionTool(name="f", description="d")  # type: ignore[call-arg]

    def test_minimal(self) -> None:
        t = UCFunctionTool(name="f", description="d", function_name="c.s.f")
        assert t.function_name == "c.s.f"


class TestPythonFunctionTool:
    """The in-process ``type: python`` tool."""

    def test_minimal_infers_schema_as_none(self) -> None:
        t = PythonFunctionTool(
            name="add_signal",
            description="Add a signal",
            callable_path="my_pkg.mod.add_signal",
        )
        assert t.type == "python"
        assert t.callable_path == "my_pkg.mod.add_signal"
        # Omitted -> None; the runtime infers the schema from the signature.
        assert t.params_json_schema is None

    def test_explicit_params_json_schema_honored(self) -> None:
        schema = {"type": "object", "properties": {"x": {"type": "integer"}}}
        t = PythonFunctionTool(
            name="f",
            description="d",
            callable_path="a.b.c",
            params_json_schema=schema,
        )
        assert t.params_json_schema == schema

    def test_requires_callable_path(self) -> None:
        with pytest.raises(ValidationError):
            PythonFunctionTool(name="f", description="d")  # type: ignore[call-arg]

    def test_does_not_emit_deprecation_warning(self) -> None:
        import warnings

        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always")
            PythonFunctionTool(name="f", description="d", callable_path="a.b")
        deprecations = [w for w in captured if issubclass(w.category, DeprecationWarning)]
        assert deprecations == []

    def test_validates_via_manifest(self) -> None:
        m = ToolManifest.model_validate(
            {
                "tools": [
                    {
                        "name": "f",
                        "type": "python",
                        "description": "d",
                        "callable_path": "a.b.c",
                    },
                ],
            }
        )
        assert isinstance(m.tools[0], PythonFunctionTool)


class TestMCPTool:
    """The new MCP-reference shape introduced in PR 5 (#111)."""

    def test_minimal(self) -> None:
        t = MCPTool(name="search_issues", description="Search GitHub issues")
        assert t.type == "mcp"
        assert t.name == "search_issues"

    def test_does_not_emit_deprecation_warning(self) -> None:
        """The ``MCPTool`` reference shape must be warning-free."""
        import warnings

        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always")
            MCPTool(name="x", description="d")
        deprecations = [w for w in captured if issubclass(w.category, DeprecationWarning)]
        assert deprecations == []

    def test_validates_via_manifest_with_explicit_type(self) -> None:
        m = ToolManifest.model_validate(
            {
                "tools": [{"name": "x", "type": "mcp", "description": "d"}],
            }
        )
        assert isinstance(m.tools[0], MCPTool)


class TestToolManifest:
    def test_empty(self) -> None:
        m = ToolManifest()
        assert m.tools == []

    def test_discriminated_union_parses_each_type(self) -> None:
        raw = {
            "tools": [
                {"name": "g", "type": "genie", "description": "d"},
                {"name": "v", "type": "vector_search", "description": "d"},
                {
                    "name": "u",
                    "type": "uc_function",
                    "description": "d",
                    "function_name": "a.b.c",
                },
                {
                    "name": "p",
                    "type": "python",
                    "description": "d",
                    "callable_path": "a.b.c",
                },
                {"name": "x", "type": "mcp", "description": "d"},
            ],
        }
        m = ToolManifest.model_validate(raw)
        assert [type(t).__name__ for t in m.tools] == [
            "GenieTool",
            "VectorSearchTool",
            "UCFunctionTool",
            "PythonFunctionTool",
            "MCPTool",
        ]

    @pytest.mark.parametrize("removed_type", ["managed_mcp", "external_mcp"])
    def test_removed_mcp_types_are_rejected(self, removed_type: str) -> None:
        with pytest.raises(ValidationError):
            ToolManifest.model_validate(
                {"tools": [{"name": "x", "type": removed_type, "description": "d", "url": "https://x"}]}
            )

    def test_unknown_type_raises(self) -> None:
        with pytest.raises(ValidationError):
            ToolManifest.model_validate(
                {
                    "tools": [
                        {"name": "x", "type": "unknown_type", "description": "d"},
                    ],
                }
            )

    def test_missing_type_raises_at_contract_layer(self) -> None:
        """Untyped entries are normalised to ``type: "mcp"`` by the
        skills *assembler* (``_normalize_tool_manifest``), not by the
        contract layer.  The raw ``ToolManifest`` therefore still
        rejects entries without a discriminator -- this guards against
        accidental untyped reads bypassing the assembler."""
        with pytest.raises(ValidationError):
            ToolManifest.model_validate(
                {
                    "tools": [{"name": "x", "description": "d"}],
                }
            )

    def test_extra_keys_ignored(self) -> None:
        """``extra='ignore'`` means typos pass but don't appear on the model."""
        t = GenieTool.model_validate(
            {
                "name": "g",
                "type": "genie",
                "description": "d",
                "descriptoin": "typo",
            }
        )
        assert not hasattr(t, "descriptoin")
