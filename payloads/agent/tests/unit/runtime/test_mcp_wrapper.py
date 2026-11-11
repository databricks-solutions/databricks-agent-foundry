"""Tests for ``bricks/agent/mcp_wrapper.py`` (issues #108 + #110).

Covers the concerns owned by the wrapper subclass:

* ``name_prefix`` round-trip between :meth:`list_tools` and
  :meth:`call_tool` (PR 2 / issue #108).
* Prompts / resources config snapshot + resource-URI matcher (PR 4 /
  issue #110).  The orchestration around these (cache, fetch, synthetic
  tools) lives in :mod:`payloads.agent.runtime.mcp_consumption` and is tested
  separately.
* Auto-poll absorption of async ``submit -> poll`` shapes plus the
  defensive short-circuit when the LLM directly calls a hidden poll
  tool.

Allow-listing and ``_meta`` injection are SDK concerns and are tested
via ``test_tools.py::TestBuildMcpServers`` (at the construction layer,
where they're configured).

All tests run offline using ``unittest.mock``; no MCP server is
contacted.  ``MCPServerStreamableHttp`` is the upstream class our
subclass extends; we patch its ``list_tools`` / ``call_tool`` to feed
controlled responses.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp.types import CallToolResult

from agent_foundry.contracts.mcp import (
    ArgPassthrough,
    AutoPollConfig,
    PromptsConfig,
    ResourcesConfig,
    ToolAllowEntry,
)
from payloads.agent.runtime.mcp_wrapper import FilteredMCPServerStreamableHttp


def _mcp_tool(name: str, annotations: dict[str, Any] | None = None) -> SimpleNamespace:
    """Build a ``mcp.types.Tool``-shaped fake supporting ``model_copy``.

    The wrapper relies on Pydantic's ``model_copy(update={"name": ...})``
    to rename without losing inputSchema.  ``SimpleNamespace`` does not
    implement that, so we build a minimal stand-in.  ``annotations`` is
    accepted to keep call-site fixtures readable but is no longer
    consumed by the wrapper.
    """

    def model_copy(*, update: dict[str, str]) -> SimpleNamespace:
        merged = {"name": name, **update}
        return SimpleNamespace(
            name=merged["name"],
            description=f"desc-of-{name}",
            inputSchema={"type": "object", "properties": {}},
            annotations=annotations,
            model_copy=model_copy,
        )

    return SimpleNamespace(
        name=name,
        description=f"desc-of-{name}",
        inputSchema={"type": "object", "properties": {}},
        annotations=annotations,
        model_copy=model_copy,
    )


def _make_server(
    prefix: str = "",
    *,
    allow_entries: list[Any] | None = None,
    prompts: PromptsConfig | None = None,
    resources: ResourcesConfig | None = None,
    auto_poll: AutoPollConfig | None = None,
    server_url: str = "https://example.invalid/mcp",
    name: str = "test-server",
) -> FilteredMCPServerStreamableHttp:
    """Construct a wrapper without touching the network.

    ``MCPServerStreamableHttp.__init__`` only validates ``params`` shape;
    it does not connect.  Connect-time logic is patched in each test.

    ``allow_entries`` defaults to ``None`` -- equivalent to "no allow
    list, admit every upstream tool" -- so tests targeting other
    behaviour (prefixing, etc.) do not have to declare an allow list
    to exercise the wrapper.
    """
    return FilteredMCPServerStreamableHttp(
        params={"url": server_url},
        name_prefix=prefix,
        allow_entries=allow_entries,
        prompts=prompts,
        resources=resources,
        auto_poll=auto_poll,
        server_url=server_url,
        cache_tools_list=False,
        name=name,
    )


def _text_result(payload: dict[str, Any]) -> CallToolResult:
    """Build a CallToolResult-shaped fake with a single JSON text item.

    Mirrors the ``mcp.types.CallToolResult`` shape consumed by the
    auto-poll loop: ``result.content`` is a list of typed content
    items; for managed Databricks servers (SQL / Genie) the receipt is
    a single ``TextContent`` whose ``text`` is JSON.  ``SimpleNamespace``
    suffices at runtime because the wrapper accesses only ``.content``
    and ``.text`` -- no Pydantic validation in that code path -- but
    we ``cast`` so callers (and ``out is result`` identity checks) see
    the real return type instead of ``SimpleNamespace``.
    """
    import json

    fake = SimpleNamespace(
        content=[SimpleNamespace(type="text", text=json.dumps(payload))],
    )
    return cast(CallToolResult, fake)


# ---------------------------------------------------------------------------
# name_prefix round-trip
# ---------------------------------------------------------------------------


class TestNamePrefixRoundTrip:
    """Verify list_tools renames and call_tool reverses the rename."""

    @pytest.mark.asyncio
    async def test_list_tools_prefixes_each_name(self) -> None:
        server = _make_server(prefix="github__")
        upstream = [_mcp_tool("search"), _mcp_tool("create_issue")]

        with patch.object(
            type(server).__mro__[1],  # MCPServerStreamableHttp
            "list_tools",
            new=AsyncMock(return_value=upstream),
        ):
            result = await server.list_tools()

        assert [t.name for t in result] == ["github__search", "github__create_issue"]

    @pytest.mark.asyncio
    async def test_list_tools_records_reverse_mapping(self) -> None:
        server = _make_server(prefix="g__")
        upstream = [_mcp_tool("search")]

        with patch.object(
            type(server).__mro__[1],
            "list_tools",
            new=AsyncMock(return_value=upstream),
        ):
            await server.list_tools()

        assert server._prefixed_to_original == {"g__search": "search"}

    @pytest.mark.asyncio
    async def test_call_tool_strips_prefix_when_listed(self) -> None:
        server = _make_server(prefix="g__")
        upstream = [_mcp_tool("search")]

        with (
            patch.object(
                type(server).__mro__[1],
                "list_tools",
                new=AsyncMock(return_value=upstream),
            ),
            patch.object(
                type(server).__mro__[1],
                "call_tool",
                new=AsyncMock(return_value="ok"),
            ) as mock_call,
        ):
            await server.list_tools()
            await server.call_tool("g__search", {"q": "issue"})

        # Forwarded with the original (un-prefixed) name; meta defaults to None.
        assert mock_call.call_args.args[0] == "search"
        assert mock_call.call_args.args[1] == {"q": "issue"}
        assert mock_call.call_args.kwargs == {"meta": None}

    @pytest.mark.asyncio
    async def test_call_tool_passes_unknown_name_through(self) -> None:
        """Defensive path: if the LLM somehow calls a name we never listed,
        forward it verbatim instead of crashing.  The upstream server will
        reject it with the canonical 'tool not found' error."""
        server = _make_server(prefix="g__")

        with patch.object(
            type(server).__mro__[1],
            "call_tool",
            new=AsyncMock(return_value="rejected"),
        ) as mock_call:
            await server.call_tool("never_listed", {"x": 1})

        assert mock_call.call_args.args[0] == "never_listed"

    @pytest.mark.asyncio
    async def test_call_tool_forwards_meta_kwarg(self) -> None:
        """The SDK's ``tool_meta_resolver`` already merged any default
        meta into the ``meta`` kwarg before reaching us; we must not
        drop it."""
        server = _make_server(prefix="g__")
        upstream = [_mcp_tool("search")]

        with (
            patch.object(
                type(server).__mro__[1],
                "list_tools",
                new=AsyncMock(return_value=upstream),
            ),
            patch.object(
                type(server).__mro__[1],
                "call_tool",
                new=AsyncMock(return_value="ok"),
            ) as mock_call,
        ):
            await server.list_tools()
            await server.call_tool("g__search", {"q": "x"}, meta={"tenant": "acme"})

        assert mock_call.call_args.kwargs == {"meta": {"tenant": "acme"}}


# ---------------------------------------------------------------------------
# Empty-prefix is a no-op
# ---------------------------------------------------------------------------


class TestEmptyPrefix:
    """Verify name_prefix='' behaves exactly like the plain SDK class."""

    @pytest.mark.asyncio
    async def test_list_tools_returns_upstream_verbatim(self) -> None:
        server = _make_server(prefix="")
        upstream = [_mcp_tool("a"), _mcp_tool("b")]

        with patch.object(
            type(server).__mro__[1],
            "list_tools",
            new=AsyncMock(return_value=upstream),
        ):
            result = await server.list_tools()

        assert result is upstream
        assert server._prefixed_to_original == {}

    @pytest.mark.asyncio
    async def test_call_tool_forwards_name_unchanged(self) -> None:
        server = _make_server(prefix="")

        with patch.object(
            type(server).__mro__[1],
            "call_tool",
            new=AsyncMock(return_value="ok"),
        ) as mock_call:
            await server.call_tool("search", {"q": "x"})

        assert mock_call.call_args.args[0] == "search"


# ---------------------------------------------------------------------------
# Mapping invalidation across list_tools refreshes
# ---------------------------------------------------------------------------


class TestMappingRebuild:
    """Verify the prefixed_to_original mapping is rebuilt atomically.

    Important when an upstream server adds/removes tools and the agent's
    cache invalidates (`MCPServerStreamableHttp.invalidate_tools_cache()`).
    """

    @pytest.mark.asyncio
    async def test_second_list_tools_replaces_mapping(self) -> None:
        server = _make_server(prefix="g__")

        first = [_mcp_tool("a"), _mcp_tool("b")]
        second = [_mcp_tool("b"), _mcp_tool("c")]

        with patch.object(
            type(server).__mro__[1],
            "list_tools",
            new=AsyncMock(side_effect=[first, second]),
        ):
            await server.list_tools()
            assert set(server._prefixed_to_original) == {"g__a", "g__b"}

            await server.list_tools()
            # 'a' was removed upstream; the mapping must drop it.
            assert set(server._prefixed_to_original) == {"g__b", "g__c"}

    @pytest.mark.asyncio
    async def test_name_prefix_is_read_only(self) -> None:
        server = _make_server(prefix="g__")
        # ``name_prefix`` is exposed as a property so callers can inspect
        # but not mutate the prefix mid-flight.
        assert server.name_prefix == "g__"
        with pytest.raises(AttributeError):
            server.name_prefix = "x__"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Shared SDK-base patcher for tests below
# ---------------------------------------------------------------------------


def _patch_super(method: str, return_value: Any) -> Any:
    """``patch.object`` on the wrapper's *base* class.

    Substitutes the upstream SDK behaviour
    (``MCPServerStreamableHttp.list_tools`` / ``call_tool``) without
    re-instantiating the wrapper, so test bodies stay focused on
    wrapper logic instead of fixture wiring.
    """
    return patch.object(
        FilteredMCPServerStreamableHttp.__mro__[1],
        method,
        new=AsyncMock(return_value=return_value),
    )


# ---------------------------------------------------------------------------
# Allow-list, glob, alias, prefix pipeline (issue: managed MCP servers
# expose UUID/catalog-prefixed tool names that don't fit a literal
# tools.allow list)
# ---------------------------------------------------------------------------
#
# The wrapper's ``list_tools`` now does the work that the SDK's
# ``tool_filter`` used to do, plus glob matching and aliasing.  These
# tests pin down the four-step pipeline:
#
#   1. ``allow_entries=None`` -> admit every upstream tool (legacy).
#   2. Bare-string entries -> exact-match filter, no rename.
#   3. ``ToolAllowEntry(match=...)`` glob -> pattern filter, name kept.
#   4. ``ToolAllowEntry(match=..., expose_as=...)`` -> alias rename
#      applied *before* ``name_prefix`` concat.
#
# All assertions speak in terms of the LLM-visible name (``visible``)
# and the upstream name (``upstream``), so the alias semantics are
# obvious from the test body.


class TestAllowEntryPipeline:
    """Verify allow / glob / alias / prefix interaction in list_tools."""

    @pytest.mark.asyncio
    async def test_no_allow_entries_admits_all_upstream(self) -> None:
        # When the deployer opts out of allow-listing entirely (legacy
        # path used by tests that focus on other behaviours) every
        # upstream tool flows through with only the prefix applied.
        from agent_foundry.contracts.mcp import ToolAllowEntry  # noqa: F401

        server = _make_server(prefix="g__", allow_entries=None)
        upstream = [_mcp_tool("alpha"), _mcp_tool("beta")]

        with _patch_super("list_tools", upstream):
            result = await server.list_tools()

        assert [t.name for t in result] == ["g__alpha", "g__beta"]
        assert server._prefixed_to_original == {"g__alpha": "alpha", "g__beta": "beta"}

    @pytest.mark.asyncio
    async def test_bare_string_entry_exact_match(self) -> None:
        from agent_foundry.contracts.mcp import ToolAllowEntry

        server = _make_server(
            prefix="g__",
            allow_entries=[ToolAllowEntry(match="alpha")],
        )
        upstream = [_mcp_tool("alpha"), _mcp_tool("beta"), _mcp_tool("alphabeta")]

        with _patch_super("list_tools", upstream):
            result = await server.list_tools()

        # Only "alpha" matches; "alphabeta" does NOT match the literal
        # "alpha" (no implicit substring matching).
        assert [t.name for t in result] == ["g__alpha"]
        assert server._prefixed_to_original == {"g__alpha": "alpha"}

    @pytest.mark.asyncio
    async def test_glob_entry_matches_multiple_upstream_tools(self) -> None:
        from agent_foundry.contracts.mcp import ToolAllowEntry

        server = _make_server(
            prefix="g__",
            allow_entries=[ToolAllowEntry(match="poll_*")],
        )
        upstream = [
            _mcp_tool("poll_a"),
            _mcp_tool("poll_b"),
            _mcp_tool("query"),
        ]

        with _patch_super("list_tools", upstream):
            result = await server.list_tools()

        # Both poll_* tools admitted; "query" filtered out.  Each
        # matched tool keeps its upstream name as the alias because
        # expose_as was not set.
        assert [t.name for t in result] == ["g__poll_a", "g__poll_b"]
        assert server._prefixed_to_original == {
            "g__poll_a": "poll_a",
            "g__poll_b": "poll_b",
        }

    @pytest.mark.asyncio
    async def test_expose_as_alias_renames_before_prefix(self) -> None:
        # The motivating case: managed Genie exposes
        # query_space_<uuid> -- collapse to a clean alias.
        from agent_foundry.contracts.mcp import ToolAllowEntry

        server = _make_server(
            prefix="genie__",
            allow_entries=[
                ToolAllowEntry(match="query_space_*", expose_as="query_space"),
            ],
        )
        upstream = [_mcp_tool("query_space_01abc")]

        with _patch_super("list_tools", upstream):
            result = await server.list_tools()

        assert [t.name for t in result] == ["genie__query_space"]
        # Reverse map keys on the LLM-visible name, value is the
        # upstream name -- so call_tool can forward correctly.
        assert server._prefixed_to_original == {
            "genie__query_space": "query_space_01abc",
        }

    @pytest.mark.asyncio
    async def test_first_matching_entry_wins(self) -> None:
        # When two entries could match, the first one in the list
        # wins.  Lets users put more-specific patterns ahead of
        # catch-alls without surprise reordering.
        from agent_foundry.contracts.mcp import ToolAllowEntry

        server = _make_server(
            prefix="g__",
            allow_entries=[
                ToolAllowEntry(match="poll_a", expose_as="specific"),
                ToolAllowEntry(match="poll_*", expose_as="generic"),
            ],
        )
        upstream = [_mcp_tool("poll_a"), _mcp_tool("poll_b")]

        with _patch_super("list_tools", upstream):
            result = await server.list_tools()

        # poll_a -> "specific" (first match wins), poll_b -> "generic".
        assert [t.name for t in result] == ["g__specific", "g__generic"]

    @pytest.mark.asyncio
    async def test_call_tool_reverse_lookup_uses_alias(self) -> None:
        # Round-trip: alias goes out, upstream comes back in.
        from agent_foundry.contracts.mcp import ToolAllowEntry

        server = _make_server(
            prefix="g__",
            allow_entries=[
                ToolAllowEntry(match="query_space_*", expose_as="query"),
            ],
        )
        upstream = [_mcp_tool("query_space_xyz", {"readOnlyHint": True})]

        with (
            _patch_super("list_tools", upstream),
            patch.object(
                FilteredMCPServerStreamableHttp.__mro__[1],
                "call_tool",
                new=AsyncMock(return_value="ok"),
            ) as mock_call,
        ):
            await server.list_tools()
            await server.call_tool("g__query", {"q": "select 1"})

        # Forwarded under the original upstream name.
        assert mock_call.call_args.args[0] == "query_space_xyz"
        assert mock_call.call_args.args[1] == {"q": "select 1"}

    @pytest.mark.asyncio
    async def test_unmatched_upstream_tools_filtered_out(self) -> None:
        # Tools that match no entry must be dropped, never silently
        # forwarded -- this is the security-sensitive surface.
        from agent_foundry.contracts.mcp import ToolAllowEntry

        server = _make_server(
            prefix="g__",
            allow_entries=[ToolAllowEntry(match="approved")],
        )
        upstream = [_mcp_tool("approved"), _mcp_tool("rogue")]

        with _patch_super("list_tools", upstream):
            result = await server.list_tools()

        assert [t.name for t in result] == ["g__approved"]
        assert "g__rogue" not in server._prefixed_to_original

    @pytest.mark.asyncio
    async def test_empty_prefix_with_alias(self) -> None:
        # name_prefix="" plus aliasing is the recommended config for
        # self-qualified upstream names (catalog__schema__func) -- the
        # alias is the LLM-visible name with no extra prefix.
        from agent_foundry.contracts.mcp import ToolAllowEntry

        server = _make_server(
            prefix="",
            allow_entries=[
                ToolAllowEntry(
                    match="cat__sch__format_byte_size",
                    expose_as="format_byte_size",
                ),
            ],
        )
        upstream = [_mcp_tool("cat__sch__format_byte_size")]

        with _patch_super("list_tools", upstream):
            result = await server.list_tools()

        assert [t.name for t in result] == ["format_byte_size"]
        assert server._prefixed_to_original == {
            "format_byte_size": "cat__sch__format_byte_size",
        }

    @pytest.mark.asyncio
    async def test_allow_entries_property_returns_copy(self) -> None:
        # Mutating the returned list must not mutate the wrapper's
        # internal state.
        from agent_foundry.contracts.mcp import ToolAllowEntry

        entries = [ToolAllowEntry(match="x")]
        server = _make_server(allow_entries=entries)
        out = server.allow_entries
        assert out is not None
        out.append(ToolAllowEntry(match="injected"))  # noqa: PT017 - mutating local
        assert len(server.allow_entries or []) == 1


# ---------------------------------------------------------------------------
# PR 4 surface: prompts / resources properties + URI matcher
# ---------------------------------------------------------------------------


class TestPromptsResourcesSnapshot:
    """The wrapper carries brick-state config for the consumption module.

    These properties exist so the consumption module never has to
    re-load brick state at request time, and so the wrapper's existing
    constructor stays the single owner of that snapshot.
    """

    def test_defaults_when_no_config_supplied(self) -> None:
        server = _make_server()
        assert server.prompts.enabled is False
        assert server.resources.enabled is False
        assert server.server_url == "https://example.invalid/mcp"

    def test_supplied_config_is_returned_unchanged(self) -> None:
        prompts = PromptsConfig(
            enabled=True,
            allow=["search_help"],
            cache="per_request",
        )
        resources = ResourcesConfig(
            enabled=True,
            allow=["file://docs/readme.md"],
            expose_as="read_tool",
        )
        server = _make_server(
            prompts=prompts,
            resources=resources,
            server_url="https://github.example/mcp",
        )
        assert server.prompts is prompts
        assert server.resources is resources
        assert server.server_url == "https://github.example/mcp"


class TestResourceURIMatcher:
    """``matches_resource_uri`` is the LLM-facing allow-list gate.

    The synthetic ``read_resource`` tool calls this before forwarding
    to the upstream server, so a malformed prompt asking for a
    non-allowed URI fails closed at the wrapper boundary.
    """

    def test_disabled_resources_match_nothing(self) -> None:
        server = _make_server(
            resources=ResourcesConfig(enabled=False, allow=["file://x"]),
        )
        assert server.matches_resource_uri("file://x") is False

    def test_exact_uri_match(self) -> None:
        server = _make_server(
            resources=ResourcesConfig(
                enabled=True,
                allow=["https://api.example/v1/health"],
            ),
        )
        assert server.matches_resource_uri("https://api.example/v1/health")
        assert not server.matches_resource_uri("https://api.example/v1/other")

    def test_glob_pattern_match(self) -> None:
        """``*`` follows ``fnmatch`` semantics -- it crosses path
        separators.  This is intentional for content-addressable
        servers (e.g. ``repo://databricks/*/README.md``); the contract
        in PR 1 explicitly calls this pattern out as the use-case."""
        server = _make_server(
            resources=ResourcesConfig(
                enabled=True,
                allow=["file://docs/*.md", "data://table/*"],
            ),
        )
        assert server.matches_resource_uri("file://docs/readme.md")
        assert server.matches_resource_uri("file://docs/api.md")
        assert server.matches_resource_uri("data://table/users")
        assert not server.matches_resource_uri("file://other/readme.md")
        # ``*`` crosses ``/`` boundaries -- this is intended by the contract.
        assert server.matches_resource_uri("file://docs/sub/nested.md")

    def test_pattern_matching_is_case_sensitive(self) -> None:
        """Most MCP resource URIs are scheme://host/path where path
        casing is significant; we mirror that here so a typo in the
        prompt cannot bypass an allow-list entry that uses different
        casing."""
        server = _make_server(
            resources=ResourcesConfig(
                enabled=True,
                allow=["file://Docs/README.md"],
            ),
        )
        assert server.matches_resource_uri("file://Docs/README.md")
        assert not server.matches_resource_uri("file://docs/readme.md")

    def test_empty_allow_matches_nothing(self) -> None:
        """A server with ``enabled=True`` but no patterns is a deploy
        bug, not a runtime crash; the matcher returns False so the
        synthetic tool surfaces a clear 'not in allow-list' error."""
        server = _make_server(
            resources=ResourcesConfig(enabled=True, allow=[]),
        )
        assert server.matches_resource_uri("anything") is False


# ---------------------------------------------------------------------------
# Auto-poll: collapse async submit -> poll into one transparent call
# ---------------------------------------------------------------------------
#
# These tests run with ``asyncio.sleep`` patched to a no-op so the
# backoff schedule does not slow the suite.  The wrapper still uses
# ``loop.time()`` for the deadline, so we patch that to a counter when
# we want to exercise the timeout branch.

_SQL_AUTO_POLL = AutoPollConfig(
    trigger="exec",
    poll_with="poll",
    forward_args=[ArgPassthrough(from_field="statement_id", to_arg="statement_id")],
    state_field="status.state",
    pending_states=["PENDING"],
    timeout_s=10.0,
    initial_interval_s=0.1,
    max_interval_s=0.5,
    backoff_factor=2.0,
)

_GENIE_AUTO_POLL = AutoPollConfig(
    trigger="query",
    poll_with="poll_resp",
    forward_args=[
        ArgPassthrough(from_field="conversation_id", to_arg="conversation_id"),
        ArgPassthrough(from_field="message_id", to_arg="message_id"),
    ],
    state_field="status",
    pending_states=["IN_PROGRESS"],
    timeout_s=30.0,
)


class TestAutoPollListTools:
    """``hide_poll_tool`` controls whether the LLM sees the poll tool."""

    @pytest.mark.asyncio
    async def test_hides_poll_tool_when_configured(self) -> None:
        # Default config has ``hide_poll_tool=True``; the poll tool
        # must vanish from the LLM-visible list while the wrapper
        # still resolves its alias internally.
        server = _make_server(prefix="sql__", auto_poll=_SQL_AUTO_POLL)
        upstream = [_mcp_tool("exec"), _mcp_tool("poll")]

        with patch.object(
            type(server).__mro__[1],
            "list_tools",
            new=AsyncMock(return_value=upstream),
        ):
            tools = await server.list_tools()

        names = [t.name for t in tools]
        assert names == ["sql__exec"]
        # Internal alias map still carries the poll tool so the
        # auto-poll loop can dispatch to it.
        assert server._prefixed_to_original["sql__poll"] == "poll"

    @pytest.mark.asyncio
    async def test_keeps_poll_tool_when_hide_false(self) -> None:
        cfg = _SQL_AUTO_POLL.model_copy(update={"hide_poll_tool": False})
        server = _make_server(prefix="sql__", auto_poll=cfg)
        upstream = [_mcp_tool("exec"), _mcp_tool("poll")]

        with patch.object(
            type(server).__mro__[1],
            "list_tools",
            new=AsyncMock(return_value=upstream),
        ):
            tools = await server.list_tools()

        assert sorted(t.name for t in tools) == ["sql__exec", "sql__poll"]


class TestAutoPollCallTool:
    """``call_tool`` for the trigger absorbs the poll cycle internally."""

    @pytest.mark.asyncio
    async def test_passthrough_when_not_trigger(self) -> None:
        # Calling a non-trigger tool must take the normal path -- no
        # auto-poll dispatch and no spurious second call_tool.
        server = _make_server(prefix="sql__", auto_poll=_SQL_AUTO_POLL)
        server._prefixed_to_original = {"sql__other": "other"}
        result = _text_result({"ok": True})
        super_call = AsyncMock(return_value=result)

        with patch.object(
            type(server).__mro__[1],
            "call_tool",
            new=super_call,
        ):
            out = await server.call_tool("sql__other", {"x": 1})

        assert out is result
        assert super_call.await_count == 1
        super_call.assert_awaited_with("other", {"x": 1}, meta=None)

    @pytest.mark.asyncio
    async def test_terminal_first_call_skips_polling(self) -> None:
        # Submit returned a non-pending state on the first call: no
        # poll fires, the wrapper returns the submit result verbatim.
        server = _make_server(prefix="sql__", auto_poll=_SQL_AUTO_POLL)
        server._prefixed_to_original = {"sql__exec": "exec", "sql__poll": "poll"}
        terminal = _text_result(
            {
                "statement_id": "abc",
                "status": {"state": "SUCCEEDED"},
                "rows": [[1]],
            }
        )
        super_call = AsyncMock(return_value=terminal)

        with patch.object(
            type(server).__mro__[1],
            "call_tool",
            new=super_call,
        ):
            out = await server.call_tool("sql__exec", {"sql": "SELECT 1"})

        assert out is terminal
        assert super_call.await_count == 1  # submit only

    @pytest.mark.asyncio
    async def test_one_poll_then_terminal(self) -> None:
        # Submit returns PENDING with a statement_id; first poll
        # returns SUCCEEDED.  The LLM-visible result is the poll
        # result, NOT the original PENDING receipt.
        server = _make_server(prefix="sql__", auto_poll=_SQL_AUTO_POLL)
        server._prefixed_to_original = {"sql__exec": "exec", "sql__poll": "poll"}
        submit = _text_result(
            {
                "statement_id": "stmt-7",
                "status": {"state": "PENDING"},
            }
        )
        terminal = _text_result(
            {
                "statement_id": "stmt-7",
                "status": {"state": "SUCCEEDED"},
                "rows": [["row1"]],
            }
        )
        super_call = AsyncMock(side_effect=[submit, terminal])

        with (
            patch.object(
                type(server).__mro__[1],
                "call_tool",
                new=super_call,
            ),
            patch("payloads.agent.runtime.mcp_wrapper.asyncio.sleep", new=AsyncMock()),
        ):
            out = await server.call_tool("sql__exec", {"sql": "..."})

        assert out is terminal
        assert super_call.await_count == 2
        # Second call must be the upstream poll tool with the
        # forwarded statement_id arg.
        super_call.assert_awaited_with(
            "poll",
            {"statement_id": "stmt-7"},
            meta=None,
        )

    @pytest.mark.asyncio
    async def test_multiple_polls_with_backoff(self) -> None:
        # Three pending results then terminal: the wrapper must
        # transparently iterate and not surface intermediate results.
        # ``asyncio.sleep`` is patched to a no-op so the backoff
        # schedule does not slow the test.
        server = _make_server(prefix="sql__", auto_poll=_SQL_AUTO_POLL)
        server._prefixed_to_original = {"sql__exec": "exec", "sql__poll": "poll"}
        pending = _text_result(
            {
                "statement_id": "s1",
                "status": {"state": "PENDING"},
            }
        )
        terminal = _text_result({"statement_id": "s1", "status": {"state": "SUCCEEDED"}, "rows": []})
        super_call = AsyncMock(
            side_effect=[pending, pending, pending, pending, terminal],
        )
        sleep_mock = AsyncMock()

        with (
            patch.object(
                type(server).__mro__[1],
                "call_tool",
                new=super_call,
            ),
            patch("payloads.agent.runtime.mcp_wrapper.asyncio.sleep", new=sleep_mock),
        ):
            out = await server.call_tool("sql__exec", {"sql": "..."})

        assert out is terminal
        assert super_call.await_count == 5
        # Backoff: 0.1, 0.2, 0.4, 0.5 (capped at max_interval_s=0.5).
        sleep_intervals = [call.args[0] for call in sleep_mock.await_args_list]
        assert sleep_intervals == [0.1, 0.2, 0.4, 0.5]

    @pytest.mark.asyncio
    async def test_timeout_returns_last_pending_result(self) -> None:
        # When the deadline expires the wrapper returns the LAST
        # pending poll result so the LLM can surface the upstream's
        # own message rather than crashing.
        cfg = _SQL_AUTO_POLL.model_copy(update={"timeout_s": 0.001})
        server = _make_server(prefix="sql__", auto_poll=cfg)
        server._prefixed_to_original = {"sql__exec": "exec", "sql__poll": "poll"}
        pending_initial = _text_result(
            {
                "statement_id": "s1",
                "status": {"state": "PENDING"},
                "message": "still working",
            }
        )
        super_call = AsyncMock(return_value=pending_initial)

        with (
            patch.object(
                type(server).__mro__[1],
                "call_tool",
                new=super_call,
            ),
            patch("payloads.agent.runtime.mcp_wrapper.asyncio.sleep", new=AsyncMock()),
        ):
            out = await server.call_tool("sql__exec", {"sql": "..."})

        # On immediate timeout (no poll attempted) the loop returns
        # the initial submit result so the LLM still sees a payload.
        assert out is pending_initial

    @pytest.mark.asyncio
    async def test_missing_forward_args_field_skips_polling(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # Submit response is pending but has no ``statement_id``: the
        # wrapper must NOT loop forever on a malformed receipt; it
        # logs a warning and returns the original payload so the LLM
        # can fall back to manual chaining.
        server = _make_server(prefix="sql__", auto_poll=_SQL_AUTO_POLL)
        server._prefixed_to_original = {"sql__exec": "exec", "sql__poll": "poll"}
        bad = _text_result(
            {
                "status": {"state": "PENDING"},
                "message": "where's the id?",
            }
        )
        super_call = AsyncMock(return_value=bad)

        with (
            patch.object(
                type(server).__mro__[1],
                "call_tool",
                new=super_call,
            ),
            caplog.at_level("WARNING", logger="payloads.agent.runtime.mcp_wrapper"),
        ):
            out = await server.call_tool("sql__exec", {})

        assert out is bad
        assert super_call.await_count == 1
        assert any("missing forward_args fields" in record.message for record in caplog.records)

    @pytest.mark.asyncio
    async def test_unparseable_submit_response_skips_polling(self) -> None:
        # Non-JSON content (or non-text content) cannot be inspected
        # for a state field; the wrapper passes the result through
        # unchanged so the LLM sees what the upstream actually said.
        server = _make_server(prefix="sql__", auto_poll=_SQL_AUTO_POLL)
        server._prefixed_to_original = {"sql__exec": "exec", "sql__poll": "poll"}
        # Same duck-typed shape as ``_text_result`` but with a
        # non-JSON ``text`` to exercise the malformed-payload branch.
        weird = cast(
            CallToolResult,
            SimpleNamespace(
                content=[
                    SimpleNamespace(type="text", text="not json at all"),
                ]
            ),
        )
        super_call = AsyncMock(return_value=weird)

        with patch.object(
            type(server).__mro__[1],
            "call_tool",
            new=super_call,
        ):
            out = await server.call_tool("sql__exec", {})

        assert out is weird
        assert super_call.await_count == 1

    @pytest.mark.asyncio
    async def test_genie_two_id_forward_args(self) -> None:
        # Genie's poll tool requires ``conversation_id`` AND
        # ``message_id``; both must be forwarded from the submit
        # response.  Verifies the forward-args list (vs. dict) shape.
        server = _make_server(prefix="g__", auto_poll=_GENIE_AUTO_POLL)
        server._prefixed_to_original = {
            "g__query": "query_space_uuid",
            "g__poll_resp": "poll_response_uuid",
        }
        submit = _text_result(
            {
                "conversation_id": "c-1",
                "message_id": "m-9",
                "status": "IN_PROGRESS",
            }
        )
        terminal = _text_result(
            {
                "conversation_id": "c-1",
                "message_id": "m-9",
                "status": "COMPLETED",
                "answer": "42",
            }
        )
        super_call = AsyncMock(side_effect=[submit, terminal])

        with (
            patch.object(
                type(server).__mro__[1],
                "call_tool",
                new=super_call,
            ),
            patch("payloads.agent.runtime.mcp_wrapper.asyncio.sleep", new=AsyncMock()),
        ):
            out = await server.call_tool("g__query", {"query": "what?"})

        assert out is terminal
        super_call.assert_awaited_with(
            "poll_response_uuid",
            {"conversation_id": "c-1", "message_id": "m-9"},
            meta=None,
        )

    @pytest.mark.asyncio
    async def test_no_auto_poll_config_passthrough(self) -> None:
        # Sanity check: with auto_poll=None the wrapper behaves
        # exactly like the SDK base + alias/prefix; no special
        # handling for trigger names (because there is no trigger).
        server = _make_server(prefix="sql__")  # auto_poll defaults to None
        server._prefixed_to_original = {"sql__exec": "exec"}
        receipt = _text_result({"statement_id": "x", "status": {"state": "PENDING"}})
        super_call = AsyncMock(return_value=receipt)

        with patch.object(
            type(server).__mro__[1],
            "call_tool",
            new=super_call,
        ):
            out = await server.call_tool("sql__exec", {})

        # The receipt is returned as-is (LLM is responsible for the
        # follow-up poll because auto_poll is disabled).
        assert out is receipt
        assert super_call.await_count == 1


class TestAutoPollPollToolBypass:
    """LLM-issued direct calls to the hidden poll tool forward straight upstream.

    Even though ``hide_poll_tool=True`` filters the tool from
    :meth:`list_tools`, chat UIs cache the tool definition across
    deploys and the LLM can hallucinate poll calls from a still-pending
    payload.  In that case we forward the call to upstream directly
    (skipping the auto-poll loop, which would recurse pointlessly on a
    poll receipt) and emit a WARNING log so the leak is auditable.
    """

    @pytest.mark.asyncio
    async def test_direct_poll_call_forwards_upstream_with_warning(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        server = _make_server(prefix="sql__", auto_poll=_SQL_AUTO_POLL)
        server._prefixed_to_original = {"sql__exec": "exec", "sql__poll": "poll"}
        terminal = _text_result(
            {
                "statement_id": "s1",
                "status": {"state": "SUCCEEDED"},
            }
        )
        super_call = AsyncMock(return_value=terminal)

        with (
            patch.object(
                type(server).__mro__[1],
                "call_tool",
                new=super_call,
            ),
            caplog.at_level("WARNING", logger="payloads.agent.runtime.mcp_wrapper"),
        ):
            out = await server.call_tool("sql__poll", {"statement_id": "s1"})

        assert out is terminal
        super_call.assert_awaited_with(
            "poll",
            {"statement_id": "s1"},
            meta=None,
        )
        # Defensive log line so we can grep for "hide_poll_tool leak"
        # incidents post-deploy.
        assert any("hidden poll tool" in record.message for record in caplog.records)

    @pytest.mark.asyncio
    async def test_direct_poll_call_skips_auto_poll_loop(self) -> None:
        # The bypass must NOT then run the auto-poll loop on the poll
        # tool itself -- that would recurse pointlessly (poll on a
        # poll receipt makes no sense).  Single super call, return
        # the result verbatim.
        server = _make_server(prefix="sql__", auto_poll=_SQL_AUTO_POLL)
        server._prefixed_to_original = {"sql__exec": "exec", "sql__poll": "poll"}
        result = _text_result(
            {
                "statement_id": "s1",
                "status": {"state": "SUCCEEDED"},
            }
        )
        super_call = AsyncMock(return_value=result)

        with patch.object(
            type(server).__mro__[1],
            "call_tool",
            new=super_call,
        ):
            out = await server.call_tool("sql__poll", {"statement_id": "s1"})

        assert out is result
        assert super_call.await_count == 1

    @pytest.mark.asyncio
    async def test_no_warning_when_hide_poll_tool_false(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # If the deployer explicitly opted out of hide_poll_tool,
        # the LLM is *expected* to be able to call the poll tool;
        # the warning would be noise.
        cfg = _SQL_AUTO_POLL.model_copy(update={"hide_poll_tool": False})
        server = _make_server(prefix="sql__", auto_poll=cfg)
        server._prefixed_to_original = {"sql__exec": "exec", "sql__poll": "poll"}
        terminal = _text_result(
            {
                "statement_id": "s1",
                "status": {"state": "SUCCEEDED"},
            }
        )
        super_call = AsyncMock(return_value=terminal)

        with (
            patch.object(
                type(server).__mro__[1],
                "call_tool",
                new=super_call,
            ),
            caplog.at_level("WARNING", logger="payloads.agent.runtime.mcp_wrapper"),
        ):
            out = await server.call_tool("sql__poll", {"statement_id": "s1"})

        assert out is terminal
        assert not any("hidden poll tool" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# Span attribute tagging for downstream scorers (issue #60)
# ---------------------------------------------------------------------------


class _RecordingSpan:
    """Minimal span stand-in that captures ``set_attribute`` calls.

    Used by the tests below to verify ``_call_tool_impl`` tags the
    active span with the right attributes without depending on a real
    MLflow exporter / OTel collector being installed.
    """

    def __init__(self) -> None:
        self.attributes: dict[str, Any] = {}

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value


class _RecordingSpanCM:
    def __init__(self, span: _RecordingSpan) -> None:
        self._span = span

    def __enter__(self) -> _RecordingSpan:
        return self._span

    def __exit__(self, *args: Any) -> None:
        return None


class TestCallToolSpanAttributes:
    """``_call_tool_impl`` tags the active span so downstream scorers
    (notably ``genie_sql_correctness``) can discover Genie calls
    without depending on operator-chosen ``name_prefix`` strings.
    """

    @pytest.mark.asyncio
    async def test_managed_genie_call_sets_tool_type_attribute(self) -> None:
        from agent_foundry.contracts.mcp import ToolAllowEntry

        server = _make_server(
            prefix="sales__",  # deliberately no 'genie' substring
            allow_entries=[ToolAllowEntry(match="query_space_*", expose_as="query_space")],
        )
        upstream = [_mcp_tool("query_space_01abc")]
        recorded = _RecordingSpan()

        with (
            _patch_super("list_tools", upstream),
            patch.object(
                FilteredMCPServerStreamableHttp.__mro__[1],
                "call_tool",
                new=AsyncMock(return_value="ok"),
            ),
            patch(
                "payloads.agent.runtime.mcp_wrapper.mlflow.start_span",
                return_value=_RecordingSpanCM(recorded),
            ),
        ):
            await server.list_tools()
            await server.call_tool("sales__query_space", {"q": "select 1"})

        assert recorded.attributes["upstream_tool_name"] == "query_space_01abc"
        assert recorded.attributes["tool_type"] == "genie"

    @pytest.mark.asyncio
    async def test_non_genie_call_does_not_set_tool_type(self) -> None:
        """Other managed servers (functions / vector search / etc.)
        must NOT be tagged ``tool_type='genie'`` -- the scorer would
        otherwise score non-Genie spans."""
        server = _make_server(prefix="g__")
        upstream = [_mcp_tool("search")]
        recorded = _RecordingSpan()

        with (
            _patch_super("list_tools", upstream),
            patch.object(
                FilteredMCPServerStreamableHttp.__mro__[1],
                "call_tool",
                new=AsyncMock(return_value="ok"),
            ),
            patch(
                "payloads.agent.runtime.mcp_wrapper.mlflow.start_span",
                return_value=_RecordingSpanCM(recorded),
            ),
        ):
            await server.list_tools()
            await server.call_tool("g__search", {"q": "x"})

        assert recorded.attributes["upstream_tool_name"] == "search"
        assert "tool_type" not in recorded.attributes


class TestSqlResultCompacting:
    """Successful execute_sql* results are compacted in call_tool."""

    @pytest.mark.asyncio
    async def test_compacts_execute_sql_read_only(self) -> None:
        server = _make_server(prefix="sql_warehouse__")
        server._prefixed_to_original = {
            "sql_warehouse__execute_sql_read_only": "execute_sql_read_only",
        }
        fat = _text_result(
            {
                "statement_id": "01-abc",
                "status": {"state": "SUCCEEDED"},
                "manifest": {
                    "format": "JSON_ARRAY",
                    "schema": {
                        "columns": [
                            {
                                "name": "n",
                                "type_text": "INT",
                                "type_name": "INT",
                                "position": 0,
                            }
                        ]
                    },
                },
                "result": {"data_array": [[42]], "row_count": 1},
            }
        )
        with patch.object(
            type(server).__mro__[1],
            "call_tool",
            new=AsyncMock(return_value=fat),
        ):
            out = await server.call_tool(
                "sql_warehouse__execute_sql_read_only",
                {"query": "SELECT 42 AS n"},
            )

        assert out is fat
        import json

        text = getattr(out.content[0], "text")
        parsed = json.loads(text)
        assert parsed == {
            "columns": ["n"],
            "rows": [[42]],
            "total_rows": 1,
            "truncated": False,
            "statement_id": "01-abc",
        }
        assert "type_text" not in text

    @pytest.mark.asyncio
    async def test_unbounded_call_skips_compacting(self) -> None:
        server = _make_server(prefix="sql_warehouse__")
        server._prefixed_to_original = {"sql_warehouse__execute_sql_read_only": "execute_sql_read_only"}
        payload = {
            "status": {"state": "SUCCEEDED"},
            "manifest": {"schema": {"columns": [{"name": "n"}]}},
            "result": {"data_array": [[i] for i in range(80)]},
        }
        fat = _text_result(payload)
        with patch.object(type(server).__mro__[1], "call_tool", new=AsyncMock(return_value=fat)) as mock_call:
            out = await server.call_tool_unbounded(
                "sql_warehouse__execute_sql_read_only", {"query": "SELECT n"}, MagicMock()
            )

        assert json.loads(getattr(out.content[0], "text")) == payload
        mock_call.assert_awaited_once_with("execute_sql_read_only", {"query": "SELECT n"}, meta=None)

    @pytest.mark.asyncio
    async def test_unbounded_call_sends_the_default_meta(self) -> None:
        server = _make_server()
        server.tool_meta_resolver = lambda _ctx: {"warehouse": "shared"}
        with patch.object(type(server).__mro__[1], "call_tool", new=AsyncMock(return_value="ok")) as mock_call:
            await server.call_tool_unbounded("execute_sql_read_only", {"query": "SELECT 1"}, MagicMock())

        mock_call.assert_awaited_once_with("execute_sql_read_only", {"query": "SELECT 1"}, meta={"warehouse": "shared"})

    def test_visible_name_follows_allow_list_and_prefix(self) -> None:
        server = _make_server(
            prefix="sql__",
            allow_entries=[ToolAllowEntry(match="execute_sql_read_only", expose_as="query")],
        )

        assert server.visible_name("execute_sql_read_only") == "sql__query"
        assert server.visible_name("execute_sql") is None

    @pytest.mark.asyncio
    async def test_non_sql_tools_unchanged(self) -> None:
        server = _make_server(prefix="docs__")
        server._prefixed_to_original = {"docs__search": "search"}
        result = _text_result({"hits": [{"title": "x"}]})
        with patch.object(
            type(server).__mro__[1],
            "call_tool",
            new=AsyncMock(return_value=result),
        ):
            out = await server.call_tool("docs__search", {"q": "x"})

        assert out is result
        assert '"hits"' in getattr(out.content[0], "text")


class TestConnectSpan:
    @pytest.mark.asyncio
    async def test_connect_is_traced_per_server(self) -> None:
        server = _make_server(prefix="g__")
        upstream_connect = AsyncMock()

        with (
            patch.object(FilteredMCPServerStreamableHttp.__mro__[1], "connect", new=upstream_connect),
            patch("payloads.agent.runtime.mcp_wrapper.mlflow.start_span") as start_span,
        ):
            await server.connect()

        start_span.assert_called_once_with(name=f"mcp.connect[{server.name}]", span_type="TASK")
        upstream_connect.assert_awaited_once()
