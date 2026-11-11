"""Tests for tool factories and skill tool dispatch."""

from __future__ import annotations

import json
from collections.abc import Coroutine
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
from agents.tool_context import ToolContext

from agent_foundry._instance_layout import resolve_payload_subdir
from agent_foundry.contracts import (
    GenieTool,
    MCPTool,
    UCFunctionTool,
    VectorSearchTool,
)
from agent_foundry.state import save_state
from agent_foundry.testing.fakes import FakeWorkspaceClient
from payloads.agent.runtime.tool_builders import create_skill_tools
from payloads.agent.runtime.tools import _execute_uc_function

BRICK_ROOT = Path(__file__).resolve().parents[3] / "runtime"
PROJECT_ROOT = BRICK_ROOT.parent.parent

# ``FunctionTool.on_invoke_tool`` is typed as ``(ctx: ToolContext[Any],
# args: str)`` but the runtime accepts ``None`` when the tool body
# doesn't dereference ``ctx``.  All these tests exercise that path;
# the cast lets us pass ``None`` once instead of sprinkling
# ``# type: ignore[arg-type]`` at every call site.
_NO_CTX: ToolContext[Any] = cast("ToolContext[Any]", None)


async def _no_sleep(_: float) -> None:
    """Drop-in for ``asyncio.sleep`` so polling tests run instantly."""
    return None


# ---------------------------------------------------------------------------
# UC function SQL execution (polling)
# ---------------------------------------------------------------------------


class TestExecuteUcFunction:
    """Cold-warehouse polling in ``_execute_uc_function`` (used by ``data_universe``)."""

    @staticmethod
    def _statement(state: str) -> SimpleNamespace:
        return SimpleNamespace(status=SimpleNamespace(state=state), statement_id="stmt-1")

    def _run(self, ws: MagicMock, max_wait_seconds: int) -> Any:
        import asyncio

        with patch("payloads.agent.runtime.tools.asyncio.sleep", new=_no_sleep):
            return asyncio.run(
                _execute_uc_function(ws, sql="SELECT 1", warehouse_id="wh-1", max_wait_seconds=max_wait_seconds)
            )

    def test_polls_pending_until_succeeded(self) -> None:
        ws = MagicMock()
        ws.statement_execution.execute_statement.return_value = self._statement("PENDING")
        ws.statement_execution.get_statement.return_value = self._statement("SUCCEEDED")

        result = self._run(ws, max_wait_seconds=180)

        ws.statement_execution.execute_statement.assert_called_once()
        assert ws.statement_execution.execute_statement.call_args.kwargs["wait_timeout"] == "50s"
        ws.statement_execution.get_statement.assert_called_with(statement_id="stmt-1")
        assert result.status.state == "SUCCEEDED"

    def test_returns_non_terminal_response_when_budget_exhausted(self) -> None:
        ws = MagicMock()
        ws.statement_execution.execute_statement.return_value = self._statement("PENDING")
        ws.statement_execution.get_statement.return_value = self._statement("PENDING")

        result = self._run(ws, max_wait_seconds=5)

        assert ws.statement_execution.execute_statement.call_args.kwargs["wait_timeout"] == "5s"
        ws.statement_execution.get_statement.assert_not_called()
        assert result.status.state == "PENDING"


# ---------------------------------------------------------------------------
# Genie and Vector Search factory — span/FunctionTool name plumbing
# ---------------------------------------------------------------------------


class TestCreateGenieTool:
    """Verify ``create_genie_tool`` uses the configured name for the
    ``FunctionTool`` (default ``"query_genie"`` for backward compat)."""

    def test_default_name_is_query_genie(self) -> None:
        from payloads.agent.runtime.tools import create_genie_tool

        ws = MagicMock()
        tool = create_genie_tool(ws, "space-xyz")
        assert tool.name == "query_genie"

    def test_name_override_propagates_to_function_tool(self) -> None:
        from payloads.agent.runtime.tools import create_genie_tool

        ws = MagicMock()
        tool = create_genie_tool(ws, "space-xyz", name="ask_swf_portfolio_planning")
        assert tool.name == "ask_swf_portfolio_planning"

    @pytest.mark.asyncio
    async def test_invoke_uses_chain_span_and_emits_sql_output(self) -> None:
        """Wrapper span must be CHAIN (not TOOL) so the SDK's
        auto-instrumented TOOL span isn't duplicated (issue #60), and
        must surface the generated SQL on its outputs/attributes so
        ``genie_sql_correctness`` can score the legacy path."""
        from payloads.agent.runtime.tools import create_genie_tool

        attachment = SimpleNamespace(
            text=SimpleNamespace(content="42 rows"),
            query=SimpleNamespace(query="SELECT COUNT(*) FROM tbl"),
            attachment_id="att-1",
        )
        msg = SimpleNamespace(
            attachments=[attachment],
            content="ignored",
            conversation_id="conv-1",
            id="msg-1",
        )

        ws = MagicMock()
        ws.genie.start_conversation_and_wait.return_value = msg
        # No rows -> formatter skips the table rendering branch.
        ws.genie.execute_message_attachment_query.return_value = SimpleNamespace(
            statement_response=SimpleNamespace(result=None, manifest=None),
        )

        tool = create_genie_tool(ws, "space-xyz", name="ask_genie")

        captured: dict[str, Any] = {"attrs": {}, "outputs": None, "span_type": None}

        class _FakeSpan:
            def set_inputs(self, _v: Any) -> None: ...
            def set_outputs(self, v: Any) -> None:
                captured["outputs"] = v

            def set_attribute(self, k: str, v: Any) -> None:
                captured["attrs"][k] = v

        class _FakeCM:
            def __init__(self, span_type: str) -> None:
                captured["span_type"] = span_type

            def __enter__(self) -> _FakeSpan:
                return _FakeSpan()

            def __exit__(self, *args: Any) -> None:
                return None

        with patch("mlflow.start_span", side_effect=lambda name, span_type: _FakeCM(span_type)):
            args_json = json.dumps({"question": "how many?"})
            ctx = cast(Any, None)  # _invoke ignores ctx
            result = await tool.on_invoke_tool(ctx, args_json)

        assert captured["span_type"] == "CHAIN"
        assert captured["attrs"]["tool_type"] == "genie"
        assert captured["attrs"]["sql"] == "SELECT COUNT(*) FROM tbl"
        assert isinstance(captured["outputs"], dict)
        assert captured["outputs"]["sql"] == "SELECT COUNT(*) FROM tbl"
        assert "SQL: SELECT COUNT(*) FROM tbl" in result

    def test_format_genie_message_returns_text_and_sql(self) -> None:
        """``_format_genie_message`` returns ``(text, sql | None)`` so
        callers can record SQL as a structured span output."""
        from payloads.agent.runtime.tools import _format_genie_message

        attachment = SimpleNamespace(
            text=SimpleNamespace(content="answer"),
            query=SimpleNamespace(query="SELECT 1"),
            attachment_id="a1",
        )
        msg = SimpleNamespace(
            attachments=[attachment],
            content="fallback",
            conversation_id="c1",
            id="m1",
        )
        ws = MagicMock()
        ws.genie.execute_message_attachment_query.return_value = SimpleNamespace(
            statement_response=SimpleNamespace(result=None, manifest=None),
        )

        text, sql = _format_genie_message(ws, msg, "space-1")
        assert sql == "SELECT 1"
        assert "SQL: SELECT 1" in text

    def test_format_genie_message_returns_none_sql_when_no_query(self) -> None:
        """Text-only messages (no SQL attachment) yield ``sql=None``."""
        from payloads.agent.runtime.tools import _format_genie_message

        attachment = SimpleNamespace(
            text=SimpleNamespace(content="just text"),
            query=None,
            attachment_id="a1",
        )
        msg = SimpleNamespace(attachments=[attachment], content="fb", conversation_id="c", id="m")
        ws = MagicMock()

        text, sql = _format_genie_message(ws, msg, "space-1")
        assert sql is None
        assert "just text" in text


class TestCreateVectorSearchTool:
    """Verify ``create_vector_search_tool`` uses the configured name for
    the RETRIEVER span, falling back to the index last segment."""

    @patch("payloads.agent.runtime.tools.VectorSearchRetrieverTool")
    def test_span_name_defaults_to_index_last_segment(self, mock_vs_cls: MagicMock) -> None:
        from payloads.agent.runtime.tools import create_vector_search_tool

        mock_instance = MagicMock()
        mock_instance.tool = {
            "function": {"name": "vs_docs", "description": "Search docs", "parameters": {}},
        }
        mock_vs_cls.return_value = mock_instance

        ws = MagicMock()
        tool = create_vector_search_tool(ws, "cat.sch.docs_index")
        # FunctionTool name is always derived from VectorSearchRetrieverTool,
        # not from the span name — we only verify span_name wiring.
        assert tool.name == "vs_docs"

    @patch("payloads.agent.runtime.tools.mlflow")
    @patch("payloads.agent.runtime.tools.VectorSearchRetrieverTool")
    def test_span_name_overridden_when_name_passed(self, mock_vs_cls: MagicMock, mock_mlflow: MagicMock) -> None:
        """When name is passed, the RETRIEVER span uses it verbatim."""
        import asyncio

        from payloads.agent.runtime.tools import create_vector_search_tool

        mock_instance = MagicMock()
        mock_instance.tool = {
            "function": {"name": "vs_docs", "description": "Search docs", "parameters": {}},
        }
        mock_instance.execute.return_value = [{"id": 1, "content": "x"}]
        mock_vs_cls.return_value = mock_instance

        span_cm = MagicMock()
        span_cm.__enter__ = MagicMock(return_value=MagicMock())
        span_cm.__exit__ = MagicMock(return_value=False)
        mock_mlflow.start_span.return_value = span_cm

        ws = MagicMock()
        tool = create_vector_search_tool(ws, "cat.sch.docs_index", name="docs_search")

        # ``on_invoke_tool`` is typed as ``Awaitable[Any]`` upstream
        # but always returns a coroutine at runtime; ``asyncio.run``
        # requires the narrower ``Coroutine`` type.
        asyncio.run(cast("Coroutine[Any, Any, Any]", tool.on_invoke_tool(_NO_CTX, json.dumps({"query": "q"}))))

        assert mock_mlflow.start_span.call_args.kwargs.get("name") == "docs_search"


# ---------------------------------------------------------------------------
# Skill tool dispatch (create_skill_tools)
# ---------------------------------------------------------------------------


class TestCreateSkillTools:
    """Verify skill tool dispatch from SKILL_TOOLS config."""

    def _make_config(self, skill_tools: list[dict[str, Any]]) -> dict[str, Any]:
        return {"SKILL_TOOLS": skill_tools}

    @patch("payloads.agent.runtime.tool_builders.resolve_tools_client")
    def test_uc_function_skipped_for_mcp(self, mock_resolve: MagicMock) -> None:
        """uc_function tools are handled via MCP, not FunctionTool."""
        mock_ws = MagicMock()
        mock_resolve.return_value = mock_ws

        config = self._make_config(
            [
                {
                    "name": "get_tickets",
                    "type": "uc_function",
                    "function_name": "cat.sch.get_tickets",
                    "description": "Pull tickets",
                }
            ]
        )

        tools = create_skill_tools(mock_ws, config, {})
        assert len(tools) == 0

    @patch("payloads.agent.runtime.tool_builders.resolve_tools_client")
    @patch("payloads.agent.runtime._tool_factory.create_genie_tool")
    def test_dispatches_genie(self, mock_genie_factory: MagicMock, mock_resolve: MagicMock) -> None:
        mock_ws = MagicMock()
        mock_resolve.return_value = mock_ws
        mock_tool = MagicMock()
        mock_tool.name = "genie_pp"
        mock_genie_factory.return_value = mock_tool

        config = self._make_config(
            [
                {
                    "name": "genie_pp",
                    "type": "genie",
                    "space_id": "space-123",
                    "description": "Query data",
                }
            ]
        )

        tools = create_skill_tools(mock_ws, config, {})
        assert len(tools) == 1
        mock_genie_factory.assert_called_once_with(mock_ws, "space-123", name="genie_pp")

    @patch("payloads.agent.runtime.tool_builders.resolve_tools_client")
    @patch("payloads.agent.runtime._tool_factory.create_vector_search_tool")
    def test_dispatches_vector_search(self, mock_vs_factory: MagicMock, mock_resolve: MagicMock) -> None:
        mock_ws = MagicMock()
        mock_resolve.return_value = mock_ws
        mock_tool = MagicMock()
        mock_tool.name = "vs_docs"
        mock_vs_factory.return_value = mock_tool

        config = self._make_config(
            [
                {
                    "name": "vs_docs",
                    "type": "vector_search",
                    "index_name": "cat.sch.docs_index",
                    "columns": ["id", "content"],
                    "query_type": "HYBRID",
                    "num_results": 5,
                    "score_threshold": 0.7,
                    "description": "Search docs",
                }
            ]
        )

        tools = create_skill_tools(mock_ws, config, {})
        assert len(tools) == 1
        mock_vs_factory.assert_called_once_with(
            mock_ws,
            "cat.sch.docs_index",
            name="vs_docs",
            columns=["id", "content"],
            query_type="HYBRID",
            num_results=5,
            score_threshold=0.7,
        )

    @patch("payloads.agent.runtime.tool_builders.resolve_tools_client")
    def test_skips_existing_tool_names(self, mock_resolve: MagicMock) -> None:
        mock_ws = MagicMock()
        mock_resolve.return_value = mock_ws

        config = self._make_config(
            [
                {
                    "name": "already_exists",
                    "type": "uc_function",
                    "function_name": "cat.sch.f",
                    "description": "test",
                }
            ]
        )

        tools = create_skill_tools(mock_ws, config, {}, existing_tool_names={"already_exists"})
        assert len(tools) == 0

    @patch("payloads.agent.runtime.tool_builders.resolve_tools_client")
    def test_mcp_brick_reference_skipped(self, mock_resolve: MagicMock) -> None:
        """``type: mcp`` tools are owned by the bricks/mcp/ runtime path,
        so create_skill_tools must not synthesise a FunctionTool for them
        (PR 5 / #111)."""
        mock_ws = MagicMock()
        mock_resolve.return_value = mock_ws

        config = self._make_config(
            [
                {
                    "name": "search_issues",
                    "type": "mcp",
                    "description": "Brick-served tool",
                }
            ]
        )

        tools = create_skill_tools(mock_ws, config, {})
        assert len(tools) == 0

    @patch("payloads.agent.runtime.tool_builders.resolve_tools_client")
    def test_empty_skill_tools_returns_empty(self, mock_resolve: MagicMock) -> None:
        mock_ws = MagicMock()
        mock_resolve.return_value = mock_ws

        tools = create_skill_tools(mock_ws, {}, {})
        assert tools == []

    @patch("payloads.agent.runtime.tool_builders.resolve_tools_client")
    @patch("payloads.agent.runtime._tool_factory.create_genie_tool")
    def test_factory_error_skips_tool(self, mock_genie: MagicMock, mock_resolve: MagicMock) -> None:
        mock_ws = MagicMock()
        mock_resolve.return_value = mock_ws
        mock_genie.side_effect = RuntimeError("Connection refused")

        config = self._make_config(
            [
                {
                    "name": "broken_genie",
                    "type": "genie",
                    "space_id": "space-123",
                    "description": "test",
                }
            ]
        )

        tools = create_skill_tools(mock_ws, config, {})
        assert len(tools) == 0

    @patch("payloads.agent.runtime.tool_builders.resolve_tools_client")
    def test_uc_function_missing_function_name_skipped(self, mock_resolve: MagicMock) -> None:
        """uc_function with no function_name fails ToolManifest validation
        in ``_as_tool`` and is dropped before reaching ``_create_skill_tool``."""
        mock_ws = MagicMock()
        mock_resolve.return_value = mock_ws

        config = self._make_config(
            [
                {
                    "name": "no_fn",
                    "type": "uc_function",
                    "description": "missing function_name",
                }
            ]
        )

        tools = create_skill_tools(mock_ws, config, {})
        assert len(tools) == 0

    @patch("payloads.agent.runtime.tool_builders.resolve_tools_client")
    def test_missing_space_id_skipped(self, mock_resolve: MagicMock) -> None:
        mock_ws = MagicMock()
        mock_resolve.return_value = mock_ws

        config = self._make_config(
            [
                {
                    "name": "no_space",
                    "type": "genie",
                    "description": "missing space_id",
                }
            ]
        )

        tools = create_skill_tools(mock_ws, config, {})
        assert len(tools) == 0


# ---------------------------------------------------------------------------
# _as_tool adapter (dict -> Tool union)
# ---------------------------------------------------------------------------

from payloads.agent.runtime.brick_mcp_servers import build_mcp_servers  # noqa: E402  # deliberate test-section split
from payloads.agent.runtime.tool_builders import _as_tool  # noqa: E402


class TestAsTool:
    """Verify the dict -> typed Tool adapter at the SKILL_TOOLS boundary."""

    def test_genie(self) -> None:
        tool = _as_tool(
            {
                "name": "g",
                "type": "genie",
                "description": "d",
                "space_id": "s-1",
            }
        )
        assert isinstance(tool, GenieTool)
        assert tool.space_id == "s-1"

    def test_vector_search(self) -> None:
        tool = _as_tool(
            {
                "name": "v",
                "type": "vector_search",
                "description": "d",
                "index_name": "c.s.i",
                "columns": ["a"],
                "query_type": "HYBRID",
                "num_results": 3,
            }
        )
        assert isinstance(tool, VectorSearchTool)
        assert tool.index_name == "c.s.i"
        assert tool.num_results == 3

    def test_uc_function(self) -> None:
        tool = _as_tool(
            {
                "name": "fn",
                "type": "uc_function",
                "description": "d",
                "function_name": "c.s.fn",
            }
        )
        assert isinstance(tool, UCFunctionTool)
        assert tool.function_name == "c.s.fn"

    def test_mcp_brick_reference(self) -> None:
        """The new {name, type: 'mcp', description} shape parses cleanly
        as ``MCPTool`` (PR 5 / #111)."""
        tool = _as_tool(
            {
                "name": "search_issues",
                "type": "mcp",
                "description": "Search GitHub issues",
            }
        )
        assert isinstance(tool, MCPTool)
        assert tool.name == "search_issues"

    @pytest.mark.parametrize("removed_type", ["managed_mcp", "external_mcp"])
    def test_removed_mcp_types_return_none(self, removed_type: str) -> None:
        tool = _as_tool(
            {
                "name": "m",
                "type": removed_type,
                "description": "d",
                "url": "https://example.com/mcp",
            }
        )
        assert tool is None

    def test_missing_type_returns_none_and_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        """``_as_tool`` is the runtime SKILL_TOOLS adapter -- it does NOT
        normalise (the assembler does that and writes ``type:`` into
        SKILL_TOOLS dicts).  An entry without ``type:`` reaching the
        runtime is therefore a malformed hand-edited config and is
        rejected with a warning."""
        with caplog.at_level("WARNING", logger="payloads.agent.runtime.core"):
            assert _as_tool({"name": "x", "description": "d"}) is None
        assert any("invalid tool config" in r.message for r in caplog.records)

    def test_unknown_type_returns_none(self) -> None:
        assert (
            _as_tool(
                {
                    "name": "x",
                    "type": "not_a_real_type",
                    "description": "d",
                }
            )
            is None
        )

    def test_uc_function_missing_function_name_returns_none(self) -> None:
        # ``function_name`` is required on UCFunctionTool -> ValidationError
        assert (
            _as_tool(
                {
                    "name": "x",
                    "type": "uc_function",
                    "description": "d",
                }
            )
            is None
        )


# ---------------------------------------------------------------------------
# MCP brick runtime path (issue #108; the only path after PR 5 / #111)
# ---------------------------------------------------------------------------


def _write_brick_state(ws: MagicMock, instance_dir: Path, server_name: str, target: str, state: dict[str, Any]) -> None:
    """Create the ``payloads/agent/mcp_servers/<server>`` dir and its workspace state.

    Providers live under the payload root, so ``build_mcp_servers``
    resolves them via
    :func:`agent_foundry._instance_layout.resolve_payload_subdir` and reads
    their state through ``agent_foundry.state.load_state``.
    """
    (resolve_payload_subdir(instance_dir, "mcp_servers") / server_name).mkdir(parents=True, exist_ok=True)
    save_state(ws, instance_dir.name, "mcp_servers", server_name, target, state)


def _brick_state(
    *,
    url: str,
    allow: list[Any],
    name_prefix: str = "",
    default_meta: dict[str, Any] | None = None,
    prompts: dict[str, Any] | None = None,
    resources: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a minimal brick state dict in the shape PR 1's deploy writes.

    ``allow`` accepts either bare strings (legacy) or dict-form entries
    (``{"match": ..., "expose_as": ...}``) so tests can exercise both
    shapes that ``_normalise_allow_entry`` has to handle.
    """
    tools: dict[str, Any] = {
        "allow": allow,
        "name_prefix": name_prefix or None,
        "default_meta": default_meta or {},
    }
    return {
        "url": url,
        "type": "managed_functions",
        "name_prefix": name_prefix,
        "tools": tools,
        "prompts": prompts if prompts is not None else {"enabled": False},
        "resources": resources if resources is not None else {"enabled": False},
    }


class TestBuildMcpServersBrickState:
    """Verify the brick-state path of ``build_mcp_servers`` (#108)."""

    def _mock_ws(self) -> MagicMock:
        ws = MagicMock()
        ws.config.host = "https://adb-123.azuredatabricks.net"
        ws.workspace = FakeWorkspaceClient().workspace
        return ws

    @patch("payloads.agent.runtime.request_context.get_user_workspace_client", return_value=None)
    @patch("payloads.agent.runtime.mcp_wrapper.MCPServerStreamableHttp.__init__", return_value=None)
    @patch("payloads.agent.runtime.brick_mcp_servers.DatabricksOAuthClientProvider")
    def test_constructs_filtered_server_from_state(
        self, mock_oauth: MagicMock, mock_super_init: MagicMock, mock_obo: MagicMock, tmp_path: Path
    ) -> None:
        from payloads.agent.runtime.mcp_wrapper import FilteredMCPServerStreamableHttp

        ws = self._mock_ws()
        _write_brick_state(
            ws,
            tmp_path,
            "github",
            "dev",
            _brick_state(
                url="https://host.example/api/2.0/mcp/functions/cat/sch",
                allow=["search_issues", "create_issue"],
                name_prefix="github__",
                default_meta={"tenant": "acme"},
            ),
        )

        with patch("payloads.agent.runtime.brick_mcp_servers.resolve_instance_dir", return_value=tmp_path):
            servers = build_mcp_servers({}, ws)

        assert len(servers) == 1
        assert isinstance(servers[0], FilteredMCPServerStreamableHttp)
        assert servers[0].name_prefix == "github__"
        # Allow / alias / prefix all live on the wrapper now (not on the
        # SDK base) -- the wrapper applies them in list_tools().  Each
        # bare-string entry from state collapses to a ToolAllowEntry
        # with match=name and no expose_as, preserving exact-match
        # backward compatibility.
        entries = servers[0].allow_entries
        assert entries is not None
        assert [e.match for e in entries] == ["search_issues", "create_issue"]
        assert all(e.expose_as is None for e in entries)
        # Super().__init__ called with brick-state url + auth +
        # tool_meta_resolver kwargs.  ``tool_filter`` is no longer
        # passed -- filtering moved into the wrapper so glob/alias
        # support can land without forking the SDK.
        kwargs = mock_super_init.call_args.kwargs
        assert kwargs["params"]["url"] == "https://host.example/api/2.0/mcp/functions/cat/sch"
        assert "tool_filter" not in kwargs
        assert callable(kwargs["tool_meta_resolver"])
        # The resolver returns the snapshotted default_meta.
        assert kwargs["tool_meta_resolver"](None) == {"tenant": "acme"}
        # name_prefix and allow_entries are consumed by the wrapper,
        # not the SDK base.
        assert "name_prefix" not in kwargs
        assert "allow_entries" not in kwargs

    @patch("payloads.agent.runtime.request_context.get_user_workspace_client", return_value=None)
    @patch("payloads.agent.runtime.mcp_wrapper.MCPServerStreamableHttp.__init__", return_value=None)
    @patch("payloads.agent.runtime.brick_mcp_servers.DatabricksOAuthClientProvider")
    def test_no_meta_resolver_when_default_meta_empty(
        self, mock_oauth: MagicMock, mock_super_init: MagicMock, mock_obo: MagicMock, tmp_path: Path
    ) -> None:
        ws = self._mock_ws()
        _write_brick_state(
            ws,
            tmp_path,
            "github",
            "dev",
            _brick_state(url="https://h/api/2.0/mcp/functions/c/s", allow=["a"]),
        )

        with patch("payloads.agent.runtime.brick_mcp_servers.resolve_instance_dir", return_value=tmp_path):
            servers = build_mcp_servers({}, ws)

        assert len(servers) == 1
        assert mock_super_init.call_args.kwargs["tool_meta_resolver"] is None

    @patch("payloads.agent.runtime.request_context.get_user_workspace_client", return_value=None)
    @patch("payloads.agent.runtime.mcp_wrapper.MCPServerStreamableHttp.__init__", return_value=None)
    @patch("payloads.agent.runtime.brick_mcp_servers.DatabricksOAuthClientProvider")
    def test_dict_form_allow_entries_normalised_into_wrapper(
        self, mock_oauth: MagicMock, mock_super_init: MagicMock, mock_obo: MagicMock, tmp_path: Path
    ) -> None:
        # State files written by the new deploy script may carry the
        # dict form ``{match, expose_as}`` inline.  ``build_mcp_servers``
        # has to feed both shapes into ToolAllowEntry without losing
        # the alias.
        from payloads.agent.runtime.mcp_wrapper import FilteredMCPServerStreamableHttp

        ws = self._mock_ws()
        _write_brick_state(
            ws,
            tmp_path,
            "genie",
            "dev",
            _brick_state(
                url="https://h/api/2.0/mcp/genie/sp_x",
                allow=[
                    "execute",  # legacy bare-string entry
                    {"match": "query_space_*", "expose_as": "query_space"},
                ],
                name_prefix="genie__",
            ),
        )

        with patch("payloads.agent.runtime.brick_mcp_servers.resolve_instance_dir", return_value=tmp_path):
            servers = build_mcp_servers({}, ws)

        assert len(servers) == 1
        assert isinstance(servers[0], FilteredMCPServerStreamableHttp)
        entries = servers[0].allow_entries
        assert entries is not None and len(entries) == 2
        # Bare-string normalised to ToolAllowEntry(match=name).
        assert entries[0].match == "execute"
        assert entries[0].expose_as is None
        # Dict form preserved through normalisation.
        assert entries[1].match == "query_space_*"
        assert entries[1].expose_as == "query_space"

    @patch("payloads.agent.runtime.request_context.get_user_workspace_client", return_value=None)
    @patch("payloads.agent.runtime.mcp_wrapper.MCPServerStreamableHttp.__init__", return_value=None)
    @patch("payloads.agent.runtime.brick_mcp_servers.DatabricksOAuthClientProvider")
    def test_invalid_allow_entry_skips_server_with_warning(
        self,
        mock_oauth: MagicMock,
        mock_super_init: MagicMock,
        mock_obo: MagicMock,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # A state file that's been hand-edited (or written by a future
        # deploy script with an unknown shape) must skip the server
        # rather than crash the whole agent at startup.
        ws = self._mock_ws()
        _write_brick_state(
            ws,
            tmp_path,
            "borked",
            "dev",
            _brick_state(
                url="https://h/api/2.0/mcp/functions/c/s",
                allow=[42],
            ),
        )

        with (
            patch("payloads.agent.runtime.brick_mcp_servers.resolve_instance_dir", return_value=tmp_path),
            caplog.at_level("WARNING", logger="payloads.agent.runtime.core"),
        ):
            servers = build_mcp_servers({}, ws)

        assert servers == []
        assert any("invalid tools.allow" in r.message for r in caplog.records)

    @patch("payloads.agent.runtime.request_context.get_user_workspace_client", return_value=None)
    @patch("payloads.agent.runtime.mcp_wrapper.MCPServerStreamableHttp.__init__", return_value=None)
    @patch("payloads.agent.runtime.brick_mcp_servers.DatabricksOAuthClientProvider")
    def test_skips_server_with_empty_allow_list(
        self,
        mock_oauth: MagicMock,
        mock_super_init: MagicMock,
        mock_obo: MagicMock,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Empty ``tools.allow`` is rejected at the runtime layer too,
        even though PR 1's contract validator should have caught it."""
        ws = self._mock_ws()
        _write_brick_state(
            ws,
            tmp_path,
            "broken",
            "dev",
            _brick_state(url="https://h/api/2.0/mcp/functions/c/s", allow=[]),
        )

        with (
            patch("payloads.agent.runtime.brick_mcp_servers.resolve_instance_dir", return_value=tmp_path),
            caplog.at_level("WARNING", logger="payloads.agent.runtime.core"),
        ):
            servers = build_mcp_servers({}, ws)

        assert len(servers) == 0
        assert any("empty tools.allow" in r.message for r in caplog.records)

    @patch("payloads.agent.runtime.request_context.get_user_workspace_client", return_value=None)
    def test_no_mcp_servers_dir_returns_empty(self, mock_obo: MagicMock, tmp_path: Path) -> None:
        with patch("payloads.agent.runtime.brick_mcp_servers.resolve_instance_dir", return_value=tmp_path):
            assert build_mcp_servers({}, self._mock_ws()) == []

    @patch("payloads.agent.runtime.request_context.get_user_workspace_client", return_value=None)
    def test_no_resolved_instance_dir_returns_empty(self, mock_obo: MagicMock) -> None:
        """Outside an instance (no INSTANCE_DIR + no foundry.toml in walk-up)
        the brick-state path is a clean no-op rather than an error."""
        with patch("payloads.agent.runtime.brick_mcp_servers.resolve_instance_dir", return_value=None):
            assert build_mcp_servers({}, self._mock_ws()) == []

    @patch("payloads.agent.runtime.request_context.get_user_workspace_client", return_value=None)
    @patch("payloads.agent.runtime.mcp_wrapper.MCPServerStreamableHttp.__init__", return_value=None)
    @patch("payloads.agent.runtime.brick_mcp_servers.DatabricksOAuthClientProvider")
    def test_iterates_multiple_servers_in_sorted_order(
        self, mock_oauth: MagicMock, mock_super_init: MagicMock, mock_obo: MagicMock, tmp_path: Path
    ) -> None:
        ws = self._mock_ws()
        _write_brick_state(
            ws,
            tmp_path,
            "zeta",
            "dev",
            _brick_state(url="https://h/api/2.0/mcp/functions/z/s", allow=["t"]),
        )
        _write_brick_state(
            ws,
            tmp_path,
            "alpha",
            "dev",
            _brick_state(url="https://h/api/2.0/mcp/functions/a/s", allow=["t"]),
        )

        with patch("payloads.agent.runtime.brick_mcp_servers.resolve_instance_dir", return_value=tmp_path):
            servers = build_mcp_servers({}, ws)

        assert len(servers) == 2
        # Sub-dirs are processed alphabetically; mirrors the convention in
        # StandardLayoutBrick.load_state_outputs and inspect.py.
        # We assert via the sequence of super().__init__ ``name`` kwargs
        # because patching the base ``__init__`` strips the ``self.name``
        # property (which reads ``self._name`` set by the real init).
        names = [c.kwargs["name"] for c in mock_super_init.call_args_list]
        assert names == ["mcp-brick-alpha", "mcp-brick-zeta"]


class TestBuildMcpServersPromptsResources:
    """Verify the brick-state path wires PromptsConfig + ResourcesConfig (#110)."""

    def _mock_ws(self) -> MagicMock:
        ws = MagicMock()
        ws.config.host = "https://adb-123.azuredatabricks.net"
        ws.workspace = FakeWorkspaceClient().workspace
        return ws

    @patch("payloads.agent.runtime.request_context.get_user_workspace_client", return_value=None)
    @patch("payloads.agent.runtime.mcp_wrapper.MCPServerStreamableHttp.__init__", return_value=None)
    @patch("payloads.agent.runtime.brick_mcp_servers.DatabricksOAuthClientProvider")
    def test_state_with_prompts_and_resources_hydrated(
        self, mock_oauth: MagicMock, mock_super_init: MagicMock, mock_obo: MagicMock, tmp_path: Path
    ) -> None:
        """The wrapper must receive the snapshotted PromptsConfig /
        ResourcesConfig and the explicit ``server_url`` so the
        consumption module can cache + match without re-loading state."""
        from payloads.agent.runtime.brick_mcp_servers import build_mcp_servers

        url = "https://h.example.com/api/2.0/mcp/external/github"
        ws = self._mock_ws()
        _write_brick_state(
            ws,
            tmp_path,
            "github",
            "dev",
            _brick_state(
                url=url,
                allow=["search"],
                name_prefix="github__",
                prompts={
                    "enabled": True,
                    "allow": ["intro", "deep_dive"],
                    "inject_into": "system_prompt",
                    "cache": "per_conversation",
                },
                resources={
                    "enabled": True,
                    "allow": ["repo://databricks/*/README.md"],
                    "expose_as": "read_tool",
                },
            ),
        )

        with patch("payloads.agent.runtime.brick_mcp_servers.resolve_instance_dir", return_value=tmp_path):
            servers = build_mcp_servers({}, ws)

        assert len(servers) == 1
        srv = servers[0]
        assert srv.prompts.enabled is True
        assert srv.prompts.allow == ["intro", "deep_dive"]
        assert srv.prompts.cache == "per_conversation"
        assert srv.resources.enabled is True
        assert srv.resources.allow == ["repo://databricks/*/README.md"]
        assert srv.resources.expose_as == "read_tool"
        # ``server_url`` is the cache key for per_conversation entries.
        assert srv.server_url == url

    @patch("payloads.agent.runtime.request_context.get_user_workspace_client", return_value=None)
    @patch("payloads.agent.runtime.mcp_wrapper.MCPServerStreamableHttp.__init__", return_value=None)
    @patch("payloads.agent.runtime.brick_mcp_servers.DatabricksOAuthClientProvider")
    def test_state_without_prompts_or_resources_uses_defaults(
        self, mock_oauth: MagicMock, mock_super_init: MagicMock, mock_obo: MagicMock, tmp_path: Path
    ) -> None:
        """Backwards-compatible: a brick state file emitted before PR 4
        has no ``prompts`` / ``resources`` keys, and we want both to
        default to ``enabled=False`` (i.e. completely inert)."""
        from payloads.agent.runtime.brick_mcp_servers import build_mcp_servers

        ws = self._mock_ws()
        _write_brick_state(
            ws,
            tmp_path,
            "github",
            "dev",
            _brick_state(
                url="https://h.example.com/api/2.0/mcp/external/github",
                allow=["x"],
            ),
        )

        with patch("payloads.agent.runtime.brick_mcp_servers.resolve_instance_dir", return_value=tmp_path):
            servers = build_mcp_servers({}, ws)

        srv = servers[0]
        assert srv.prompts.enabled is False
        assert srv.resources.enabled is False

    @patch("payloads.agent.runtime.request_context.get_user_workspace_client", return_value=None)
    @patch("payloads.agent.runtime.mcp_wrapper.MCPServerStreamableHttp.__init__", return_value=None)
    @patch("payloads.agent.runtime.brick_mcp_servers.DatabricksOAuthClientProvider")
    def test_invalid_prompts_config_skips_server(
        self,
        mock_oauth: MagicMock,
        mock_super_init: MagicMock,
        mock_obo: MagicMock,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A hand-edited state file with a bad ``cache:`` value (e.g.
        ``per_process``, which the contract rejects) is caught at
        runtime and the server is skipped with a warning."""
        from payloads.agent.runtime.brick_mcp_servers import build_mcp_servers

        ws = self._mock_ws()
        _write_brick_state(
            ws,
            tmp_path,
            "broken",
            "dev",
            _brick_state(
                url="https://h.example.com/api/2.0/mcp/external/x",
                allow=["x"],
                prompts={
                    "enabled": True,
                    "allow": ["a"],
                    "cache": "per_process",  # not a valid Literal
                },
            ),
        )

        with (
            patch("payloads.agent.runtime.brick_mcp_servers.resolve_instance_dir", return_value=tmp_path),
            caplog.at_level("WARNING", logger="payloads.agent.runtime.core"),
        ):
            servers = build_mcp_servers({}, ws)

        assert servers == []
        assert any("invalid prompts/resources" in r.message for r in caplog.records)


class TestCreateToolsListForm:
    """Regression: ``create_tools`` must iterate the list-of-``Tool``-dicts
    shape that ``AgentConfig.tools_typed`` produces via ``model_dump``
    (67-E / PR #80).  Before this fix the function did
    ``config["TOOLS"].get("genie", {})`` on what was already a list,
    raising ``AttributeError: 'list' object has no attribute 'get'`` on
    every request the agent served.
    """

    @staticmethod
    def _mock_resolve(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
        ws = MagicMock()
        monkeypatch.setattr(
            "payloads.agent.runtime.tool_builders.resolve_tools_client",
            lambda _sp: ws,
        )
        return ws

    def test_empty_tools_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``[]`` is the common case for instances that declare no
        agent-level tools (they use skill-level tools instead).  Must not
        raise."""
        from payloads.agent.runtime.tool_builders import create_tools

        self._mock_resolve(monkeypatch)
        result = create_tools(MagicMock(), {"TOOLS": []}, {})
        assert result == []

    def test_missing_tools_key_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """If ``TOOLS`` is absent entirely, fall back to empty."""
        from payloads.agent.runtime.tool_builders import create_tools

        self._mock_resolve(monkeypatch)
        assert create_tools(MagicMock(), {}, {}) == []

    def test_dict_form_is_rejected_with_warning(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Accidentally passing the legacy dict form must not crash; it
        should log a migration hint and return an empty tool list."""
        from payloads.agent.runtime.tool_builders import create_tools

        self._mock_resolve(monkeypatch)
        legacy = {"TOOLS": {"genie": {"enabled": True, "space_id": "s"}}}
        with caplog.at_level("WARNING", logger="payloads.agent.runtime.core"):
            result = create_tools(MagicMock(), legacy, {})
        assert result == []
        assert any("unexpected TOOLS shape" in r.message for r in caplog.records)

    @patch("payloads.agent.runtime._tool_factory.create_genie_tool")
    def test_genie_tool_from_list_form(self, mock_genie: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
        from payloads.agent.runtime.tool_builders import create_tools

        self._mock_resolve(monkeypatch)
        mock_genie.return_value = SimpleNamespace(name="query_genie")
        result = create_tools(
            MagicMock(),
            {"TOOLS": [{"type": "genie", "space_id": "space-abc"}]},
            {},
        )
        assert len(result) == 1
        mock_genie.assert_called_once()
        assert mock_genie.call_args[0][1] == "space-abc"

    @patch("payloads.agent.runtime._tool_factory.create_genie_tool")
    def test_genie_space_id_falls_back_to_variables(
        self, mock_genie: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the tool dict has no ``space_id``, pull it from
        ``variables["genie_space_id"]`` (populated by the orchestrator's
        state-preload step)."""
        from payloads.agent.runtime.tool_builders import create_tools

        self._mock_resolve(monkeypatch)
        mock_genie.return_value = SimpleNamespace(name="query_genie")
        result = create_tools(
            MagicMock(),
            {"TOOLS": [{"type": "genie"}]},
            {"genie_space_id": "space-from-vars"},
        )
        assert len(result) == 1
        assert mock_genie.call_args[0][1] == "space-from-vars"

    @patch("payloads.agent.runtime._tool_factory.create_genie_tool")
    def test_genie_without_space_id_is_skipped(
        self, mock_genie: MagicMock, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        from payloads.agent.runtime.tool_builders import create_tools

        self._mock_resolve(monkeypatch)
        with caplog.at_level("WARNING", logger="payloads.agent.runtime.core"):
            result = create_tools(MagicMock(), {"TOOLS": [{"type": "genie"}]}, {})
        assert result == []
        mock_genie.assert_not_called()

    @patch("payloads.agent.runtime._tool_factory.create_vector_search_tool")
    def test_vector_search_tool_from_list_form(self, mock_vs: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
        from payloads.agent.runtime.tool_builders import create_tools

        self._mock_resolve(monkeypatch)
        mock_vs.return_value = SimpleNamespace(name="query_vector_search")
        result = create_tools(
            MagicMock(),
            {
                "TOOLS": [
                    {
                        "type": "vector_search",
                        "index_name": "cat.sch.idx",
                        "columns": ["id", "text"],
                        "query_type": "HYBRID",
                        "num_results": 5,
                        "score_threshold": 0.5,
                    }
                ]
            },
            {},
        )
        assert len(result) == 1
        mock_vs.assert_called_once()
        # Positional: ws, index_name.  Kwargs: columns / query_type / etc.
        assert mock_vs.call_args[0][1] == "cat.sch.idx"
        kwargs = mock_vs.call_args[1]
        assert kwargs["columns"] == ["id", "text"]
        assert kwargs["query_type"] == "HYBRID"
        assert kwargs["num_results"] == 5
        assert kwargs["score_threshold"] == pytest.approx(0.5)

    @patch("payloads.agent.runtime._tool_factory.create_vector_search_tool")
    @patch("payloads.agent.runtime._tool_factory.create_genie_tool")
    def test_mixed_tool_list_creates_all(
        self, mock_genie: MagicMock, mock_vs: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from payloads.agent.runtime.tool_builders import create_tools

        self._mock_resolve(monkeypatch)
        mock_genie.return_value = SimpleNamespace(name="query_genie")
        mock_vs.return_value = SimpleNamespace(name="query_vector_search")
        result = create_tools(
            MagicMock(),
            {
                "TOOLS": [
                    {"type": "genie", "space_id": "s"},
                    {"type": "vector_search", "index_name": "c.s.i"},
                ]
            },
            {},
        )
        assert len(result) == 2

    @patch("payloads.agent.runtime._tool_factory.create_genie_tool", side_effect=RuntimeError("boom"))
    def test_genie_creation_error_is_skipped(
        self, mock_genie: MagicMock, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        from payloads.agent.runtime.tool_builders import create_tools

        self._mock_resolve(monkeypatch)
        with caplog.at_level("WARNING", logger="payloads.agent.runtime.core"):
            result = create_tools(
                MagicMock(),
                {"TOOLS": [{"type": "genie", "space_id": "s"}]},
                {},
            )
        assert result == []

    def test_unknown_tool_type_is_silently_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """UC-function / MCP tool types are handled by other code paths
        (skills, build_mcp_servers).  ``create_tools`` must skip them
        without raising so agent requests don't fail."""
        from payloads.agent.runtime.tool_builders import create_tools

        self._mock_resolve(monkeypatch)
        result = create_tools(
            MagicMock(),
            {
                "TOOLS": [
                    {"type": "uc_function", "function_name": "c.s.f"},
                    {"type": "managed_mcp", "url": "https://..."},
                    {"type": "external_mcp", "url": "https://..."},
                ]
            },
            {},
        )
        assert result == []

    def test_non_dict_entries_are_skipped(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        from payloads.agent.runtime.tool_builders import create_tools

        self._mock_resolve(monkeypatch)
        with caplog.at_level("WARNING", logger="payloads.agent.runtime.core"):
            result = create_tools(
                MagicMock(),
                {"TOOLS": ["not-a-dict", 42, None]},
                {},
            )
        assert result == []

    def test_produces_list_form_that_matches_tools_typed_dump(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """End-to-end: ``AgentConfig.tools_typed`` dumped to JSON-compatible
        dicts is exactly what ``create_tools`` consumes."""
        from payloads.agent.runtime.tool_builders import create_tools

        # Build via the actual typed contracts, model_dump, then feed in.
        typed = [
            GenieTool(name="query_genie", description="...", space_id="space-1"),
            VectorSearchTool(
                name="query_vs",
                description="...",
                index_name="c.s.idx",
            ),
        ]
        tools_cfg = [t.model_dump(mode="json") for t in typed]

        self._mock_resolve(monkeypatch)
        with (
            patch("payloads.agent.runtime._tool_factory.create_genie_tool") as mock_g,
            patch("payloads.agent.runtime._tool_factory.create_vector_search_tool") as mock_v,
        ):
            mock_g.return_value = SimpleNamespace(name="query_genie")
            mock_v.return_value = SimpleNamespace(name="query_vector_search")
            result = create_tools(MagicMock(), {"TOOLS": tools_cfg}, {})

        assert len(result) == 2
