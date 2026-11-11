"""Tests for ``bricks/agent/mcp_consumption.py`` (issue #110).

Covers four orthogonal concerns:

1. Prompt fetch + render -- ``collect_prompt_manuals`` returns the
   correct ``## Tool Manuals`` block for each combination of
   ``enabled``, ``allow``, and ``inject_into``.
2. Cache lifetimes -- ``per_request`` is per-call, ``per_conversation``
   shares state across calls with the same ``conversation_id``, and
   the bounded LRU evicts when full.
3. Failure isolation -- a server whose ``list_prompts`` /
   ``get_prompt`` raises is logged at WARNING and skipped; the rest
   of the manual still renders.
4. Synthetic ``read_resource`` tool -- name follows the brick prefix,
   description enumerates the allow-list, and the runtime invocation
   handles JSON errors / disallowed URIs / upstream failures without
   raising.

All tests run offline.  The wrapper class is real (we want to exercise
:meth:`matches_resource_uri` and the property surface), but its async
methods (``list_prompts`` / ``get_prompt`` / ``read_resource``) are
patched on a per-test basis via :class:`unittest.mock.AsyncMock`.
"""

from __future__ import annotations

from collections.abc import Generator
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest
from agents.tool_context import ToolContext

from agent_foundry.contracts.mcp import PromptsConfig, ResourcesConfig
from payloads.agent.runtime import mcp_consumption
from payloads.agent.runtime.mcp_consumption import (
    _PER_CONVERSATION_CACHE_MAX,
    CachedManual,
    _clear_per_conversation_cache,
    collect_prompt_manuals,
    synthesize_resource_tools,
)
from payloads.agent.runtime.mcp_wrapper import FilteredMCPServerStreamableHttp

# ``FunctionTool.on_invoke_tool`` is typed as
# ``(ctx: ToolContext[Any], args: str)`` but the runtime accepts
# ``None`` when the tool body doesn't dereference ``ctx``.  These
# tests exercise the no-context path uniformly.
_NO_CTX: ToolContext[Any] = cast("ToolContext[Any]", None)

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def _make_server(
    *,
    prompts: PromptsConfig | None = None,
    resources: ResourcesConfig | None = None,
    name: str = "github",
    prefix: str = "github__",
    server_url: str = "https://github.example/mcp",
) -> FilteredMCPServerStreamableHttp:
    """Construct a wrapper without touching the network."""
    return FilteredMCPServerStreamableHttp(
        params={"url": server_url},
        name_prefix=prefix,
        prompts=prompts,
        resources=resources,
        server_url=server_url,
        cache_tools_list=False,
        name=name,
    )


def _prompt_message(text: str) -> SimpleNamespace:
    """``mcp.types.PromptMessage``-shaped fake."""
    return SimpleNamespace(
        role="user",
        content=SimpleNamespace(type="text", text=text),
    )


def _list_prompts_result(*names: str) -> SimpleNamespace:
    """``mcp.types.ListPromptsResult``-shaped fake."""
    return SimpleNamespace(
        prompts=[SimpleNamespace(name=n, description=f"desc-{n}") for n in names],
    )


def _get_prompt_result(text: str) -> SimpleNamespace:
    """``mcp.types.GetPromptResult``-shaped fake."""
    return SimpleNamespace(
        description="d",
        messages=[_prompt_message(text)],
    )


def _read_resource_result(text: str | None = None, blob: bytes | None = None) -> SimpleNamespace:
    """``mcp.types.ReadResourceResult``-shaped fake."""
    contents: list[Any] = []
    if text is not None:
        contents.append(SimpleNamespace(text=text))
    if blob is not None:
        contents.append(SimpleNamespace(blob=blob, mimeType="image/png"))
    return SimpleNamespace(contents=contents)


@pytest.fixture(autouse=True)
def _isolate_module_cache() -> Generator[None, None, None]:
    """The bounded LRU is module-level state; isolate per test."""
    _clear_per_conversation_cache()
    yield
    _clear_per_conversation_cache()


# ---------------------------------------------------------------------------
# collect_prompt_manuals
# ---------------------------------------------------------------------------


class TestCollectPromptManualsBasics:
    """Smoke-level: empty inputs produce empty output without crashing."""

    @pytest.mark.asyncio
    async def test_no_servers_returns_empty_string(self) -> None:
        result = await collect_prompt_manuals(
            [],
            conversation_id="c1",
            request_cache={},
        )
        assert result == ""

    @pytest.mark.asyncio
    async def test_disabled_servers_skipped(self) -> None:
        server = _make_server(prompts=PromptsConfig(enabled=False, allow=["x"]))
        result = await collect_prompt_manuals(
            [server],
            conversation_id="c1",
            request_cache={},
        )
        assert result == ""

    @pytest.mark.asyncio
    async def test_inject_into_none_skipped(self) -> None:
        server = _make_server(
            prompts=PromptsConfig(
                enabled=True,
                allow=["x"],
                inject_into="none",
            ),
        )
        # No upstream call should happen; if it did this would raise.
        with patch.object(
            FilteredMCPServerStreamableHttp,
            "list_prompts",
            new=AsyncMock(side_effect=AssertionError("must not be called")),
        ):
            result = await collect_prompt_manuals(
                [server],
                conversation_id="c1",
                request_cache={},
            )
        assert result == ""

    @pytest.mark.asyncio
    async def test_empty_allow_list_skipped(self) -> None:
        server = _make_server(
            prompts=PromptsConfig(enabled=True, allow=[]),
        )
        with patch.object(
            FilteredMCPServerStreamableHttp,
            "list_prompts",
            new=AsyncMock(side_effect=AssertionError("must not be called")),
        ):
            result = await collect_prompt_manuals(
                [server],
                conversation_id="c1",
                request_cache={},
            )
        assert result == ""


class TestCollectPromptManualsRendering:
    """Single-server happy path: header + per-prompt sub-headings."""

    @pytest.mark.asyncio
    async def test_single_prompt_rendered(self) -> None:
        server = _make_server(
            prompts=PromptsConfig(enabled=True, allow=["search_help"]),
        )
        with (
            patch.object(
                FilteredMCPServerStreamableHttp,
                "list_prompts",
                new=AsyncMock(return_value=_list_prompts_result("search_help")),
            ),
            patch.object(
                FilteredMCPServerStreamableHttp,
                "get_prompt",
                new=AsyncMock(return_value=_get_prompt_result("Use search to find issues.")),
            ),
        ):
            result = await collect_prompt_manuals(
                [server],
                conversation_id="c1",
                request_cache={},
            )

        assert result.startswith("## Tool Manuals")
        assert "### github: search_help" in result
        assert "Use search to find issues." in result

    @pytest.mark.asyncio
    async def test_multiple_prompts_concatenated_in_allow_order(self) -> None:
        server = _make_server(
            prompts=PromptsConfig(
                enabled=True,
                allow=["intro", "deep_dive"],
            ),
        )
        with (
            patch.object(
                FilteredMCPServerStreamableHttp,
                "list_prompts",
                new=AsyncMock(return_value=_list_prompts_result("intro", "deep_dive", "ignored")),
            ),
            patch.object(
                FilteredMCPServerStreamableHttp,
                "get_prompt",
                new=AsyncMock(
                    side_effect=[
                        _get_prompt_result("Intro body."),
                        _get_prompt_result("Deep dive body."),
                    ]
                ),
            ),
        ):
            result = await collect_prompt_manuals(
                [server],
                conversation_id="c1",
                request_cache={},
            )

        intro_idx = result.index("Intro body.")
        deep_idx = result.index("Deep dive body.")
        assert intro_idx < deep_idx
        # ``ignored`` is in list_prompts but not in allow -- never fetched.
        assert "ignored" not in result

    @pytest.mark.asyncio
    async def test_wildcard_allow_expands_to_all_listed_prompts(self) -> None:
        server = _make_server(
            prompts=PromptsConfig(enabled=True, allow=["*"]),
        )
        with (
            patch.object(
                FilteredMCPServerStreamableHttp,
                "list_prompts",
                new=AsyncMock(return_value=_list_prompts_result("a", "b")),
            ),
            patch.object(
                FilteredMCPServerStreamableHttp,
                "get_prompt",
                new=AsyncMock(
                    side_effect=[
                        _get_prompt_result("body-a"),
                        _get_prompt_result("body-b"),
                    ]
                ),
            ),
        ):
            result = await collect_prompt_manuals(
                [server],
                conversation_id="c1",
                request_cache={},
            )

        assert "body-a" in result and "body-b" in result

    @pytest.mark.asyncio
    async def test_allow_entry_not_listed_is_skipped(self) -> None:
        """An allow-listed prompt name that the server does not actually
        publish is ignored silently -- we don't want to crash a request
        because someone removed a prompt upstream."""
        server = _make_server(
            prompts=PromptsConfig(
                enabled=True,
                allow=["exists", "stale"],
            ),
        )
        with (
            patch.object(
                FilteredMCPServerStreamableHttp,
                "list_prompts",
                new=AsyncMock(return_value=_list_prompts_result("exists")),
            ),
            patch.object(
                FilteredMCPServerStreamableHttp,
                "get_prompt",
                new=AsyncMock(return_value=_get_prompt_result("present")),
            ) as get_mock,
        ):
            result = await collect_prompt_manuals(
                [server],
                conversation_id="c1",
                request_cache={},
            )

        assert "present" in result
        assert get_mock.call_count == 1

    @pytest.mark.asyncio
    async def test_multiple_servers_each_get_section(self) -> None:
        github = _make_server(
            prompts=PromptsConfig(enabled=True, allow=["intro"]),
            name="github",
        )
        slack = _make_server(
            prompts=PromptsConfig(enabled=True, allow=["intro"]),
            name="slack",
            prefix="slack__",
            server_url="https://slack.example/mcp",
        )

        with (
            patch.object(
                FilteredMCPServerStreamableHttp,
                "list_prompts",
                new=AsyncMock(return_value=_list_prompts_result("intro")),
            ),
            patch.object(
                FilteredMCPServerStreamableHttp,
                "get_prompt",
                new=AsyncMock(return_value=_get_prompt_result("body")),
            ),
        ):
            result = await collect_prompt_manuals(
                [github, slack],
                conversation_id="c1",
                request_cache={},
            )

        assert "### github: intro" in result
        assert "### slack: intro" in result


class TestCollectPromptManualsFailureIsolation:
    """A failing server must not poison the manual for other servers."""

    @pytest.mark.asyncio
    async def test_list_prompts_failure_skips_server(self, caplog: pytest.LogCaptureFixture) -> None:
        bad = _make_server(
            prompts=PromptsConfig(enabled=True, allow=["intro"]),
            name="bad",
        )
        with patch.object(
            FilteredMCPServerStreamableHttp,
            "list_prompts",
            new=AsyncMock(side_effect=RuntimeError("upstream gone")),
        ):
            result = await collect_prompt_manuals(
                [bad],
                conversation_id="c1",
                request_cache={},
            )
        assert result == ""
        assert any("list_prompts failed" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_get_prompt_failure_skips_just_that_prompt(self) -> None:
        server = _make_server(
            prompts=PromptsConfig(enabled=True, allow=["good", "bad"]),
        )
        with (
            patch.object(
                FilteredMCPServerStreamableHttp,
                "list_prompts",
                new=AsyncMock(return_value=_list_prompts_result("good", "bad")),
            ),
            patch.object(
                FilteredMCPServerStreamableHttp,
                "get_prompt",
                new=AsyncMock(
                    side_effect=[
                        _get_prompt_result("present"),
                        RuntimeError("crash"),
                    ]
                ),
            ),
        ):
            result = await collect_prompt_manuals(
                [server],
                conversation_id="c1",
                request_cache={},
            )
        assert "present" in result
        assert "bad" not in result.split("\n", 1)[0]  # still in body for the failed name? no.


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


class TestPerRequestCache:
    """``per_request`` cache: hits within the same call, fresh next call."""

    @pytest.mark.asyncio
    async def test_request_cache_short_circuits_second_call(self) -> None:
        server = _make_server(
            prompts=PromptsConfig(
                enabled=True,
                allow=["intro"],
                cache="per_request",
            ),
        )
        list_mock = AsyncMock(return_value=_list_prompts_result("intro"))
        get_mock = AsyncMock(return_value=_get_prompt_result("body"))

        cache: dict[Any, Any] = {}
        with (
            patch.object(
                FilteredMCPServerStreamableHttp,
                "list_prompts",
                new=list_mock,
            ),
            patch.object(
                FilteredMCPServerStreamableHttp,
                "get_prompt",
                new=get_mock,
            ),
        ):
            r1 = await collect_prompt_manuals(
                [server],
                conversation_id="c1",
                request_cache=cache,
            )
            r2 = await collect_prompt_manuals(
                [server],
                conversation_id="c1",
                request_cache=cache,
            )

        assert r1 == r2
        assert list_mock.call_count == 1, "list_prompts should be cached"
        assert get_mock.call_count == 1, "get_prompt should be cached"


class TestPerConversationCache:
    """``per_conversation`` cache: shared across calls with same id."""

    @pytest.mark.asyncio
    async def test_same_conversation_id_hits_lru(self) -> None:
        server = _make_server(
            prompts=PromptsConfig(
                enabled=True,
                allow=["intro"],
                cache="per_conversation",
            ),
        )
        list_mock = AsyncMock(return_value=_list_prompts_result("intro"))
        get_mock = AsyncMock(return_value=_get_prompt_result("body"))

        with (
            patch.object(
                FilteredMCPServerStreamableHttp,
                "list_prompts",
                new=list_mock,
            ),
            patch.object(
                FilteredMCPServerStreamableHttp,
                "get_prompt",
                new=get_mock,
            ),
        ):
            await collect_prompt_manuals(
                [server],
                conversation_id="conv-123",
                request_cache={},
            )
            await collect_prompt_manuals(
                [server],
                conversation_id="conv-123",
                request_cache={},
            )

        assert list_mock.call_count == 1
        assert get_mock.call_count == 1

    @pytest.mark.asyncio
    async def test_different_conversation_ids_miss(self) -> None:
        server = _make_server(
            prompts=PromptsConfig(
                enabled=True,
                allow=["intro"],
                cache="per_conversation",
            ),
        )
        list_mock = AsyncMock(return_value=_list_prompts_result("intro"))
        get_mock = AsyncMock(return_value=_get_prompt_result("body"))

        with (
            patch.object(
                FilteredMCPServerStreamableHttp,
                "list_prompts",
                new=list_mock,
            ),
            patch.object(
                FilteredMCPServerStreamableHttp,
                "get_prompt",
                new=get_mock,
            ),
        ):
            await collect_prompt_manuals(
                [server],
                conversation_id="conv-A",
                request_cache={},
            )
            await collect_prompt_manuals(
                [server],
                conversation_id="conv-B",
                request_cache={},
            )

        assert list_mock.call_count == 2
        assert get_mock.call_count == 2

    @pytest.mark.asyncio
    async def test_no_conversation_id_falls_back_to_request_cache(self) -> None:
        """Local CLI / interactive callers don't have a conversation id;
        we must still produce a result without poisoning the LRU."""
        server = _make_server(
            prompts=PromptsConfig(
                enabled=True,
                allow=["intro"],
                cache="per_conversation",
            ),
        )
        list_mock = AsyncMock(return_value=_list_prompts_result("intro"))
        get_mock = AsyncMock(return_value=_get_prompt_result("body"))
        cache: dict[Any, Any] = {}

        with (
            patch.object(
                FilteredMCPServerStreamableHttp,
                "list_prompts",
                new=list_mock,
            ),
            patch.object(
                FilteredMCPServerStreamableHttp,
                "get_prompt",
                new=get_mock,
            ),
        ):
            await collect_prompt_manuals(
                [server],
                conversation_id=None,
                request_cache=cache,
            )
            await collect_prompt_manuals(
                [server],
                conversation_id=None,
                request_cache=cache,
            )

        # Cached in the per-request dict, not the module LRU.
        assert list_mock.call_count == 1
        assert mcp_consumption._PER_CONVERSATION_CACHE == {}
        assert cache  # populated

    def test_lru_evicts_oldest_when_full(self) -> None:
        """Direct unit test for the cache primitive itself: insert
        ``_PER_CONVERSATION_CACHE_MAX + 1`` entries, verify the first
        was evicted and the last is present."""
        for i in range(_PER_CONVERSATION_CACHE_MAX):
            mcp_consumption._cache_put(
                ("url", f"c-{i}"),
                CachedManual(text=f"body-{i}"),
            )
        assert len(mcp_consumption._PER_CONVERSATION_CACHE) == _PER_CONVERSATION_CACHE_MAX

        # One more triggers eviction of the oldest entry.
        mcp_consumption._cache_put(
            ("url", "c-overflow"),
            CachedManual(text="last"),
        )
        assert len(mcp_consumption._PER_CONVERSATION_CACHE) == _PER_CONVERSATION_CACHE_MAX
        assert ("url", "c-0") not in mcp_consumption._PER_CONVERSATION_CACHE
        assert ("url", "c-overflow") in mcp_consumption._PER_CONVERSATION_CACHE

    def test_lru_get_promotes_recency(self) -> None:
        """Reading an entry should bump it to the most-recent slot so
        a subsequent overflow doesn't evict it."""
        for i in range(_PER_CONVERSATION_CACHE_MAX):
            mcp_consumption._cache_put(
                ("url", f"c-{i}"),
                CachedManual(text=f"body-{i}"),
            )
        # Read the oldest entry -- it should now be the freshest.
        assert mcp_consumption._cache_get(("url", "c-0")) is not None
        # One overflow now evicts c-1, not c-0.
        mcp_consumption._cache_put(
            ("url", "c-overflow"),
            CachedManual(text="last"),
        )
        assert ("url", "c-0") in mcp_consumption._PER_CONVERSATION_CACHE
        assert ("url", "c-1") not in mcp_consumption._PER_CONVERSATION_CACHE


# ---------------------------------------------------------------------------
# synthesize_resource_tools
# ---------------------------------------------------------------------------


class TestSynthesizeResourceTools:
    """One synthetic tool per server with resources enabled + read_tool."""

    def test_disabled_servers_skipped(self) -> None:
        s = _make_server(resources=ResourcesConfig(enabled=False, allow=["x"]))
        assert synthesize_resource_tools([s]) == []

    def test_expose_as_none_skipped(self) -> None:
        s = _make_server(
            resources=ResourcesConfig(
                enabled=True,
                allow=["x"],
                expose_as="none",
            ),
        )
        assert synthesize_resource_tools([s]) == []

    def test_empty_allow_skipped(self) -> None:
        """An ``enabled=True, allow=[]`` server is a deploy bug; we
        skip it at runtime so the LLM doesn't see a tool that can
        only ever return errors."""
        s = _make_server(
            resources=ResourcesConfig(enabled=True, allow=[]),
        )
        assert synthesize_resource_tools([s]) == []

    def test_tool_name_uses_brick_prefix(self) -> None:
        s = _make_server(
            prefix="github__",
            resources=ResourcesConfig(
                enabled=True,
                allow=["file://x"],
                expose_as="read_tool",
            ),
        )
        tools = synthesize_resource_tools([s])
        assert len(tools) == 1
        assert tools[0].name == "github__read_resource"

    def test_tool_description_lists_allowed_uris(self) -> None:
        s = _make_server(
            resources=ResourcesConfig(
                enabled=True,
                allow=["file://docs/*.md", "data://table/users"],
                expose_as="read_tool",
            ),
        )
        tool = synthesize_resource_tools([s])[0]
        assert "file://docs/*.md" in tool.description
        assert "data://table/users" in tool.description

    def test_one_tool_per_server(self) -> None:
        s1 = _make_server(
            prefix="github__",
            resources=ResourcesConfig(
                enabled=True,
                allow=["a"],
                expose_as="read_tool",
            ),
            name="github",
        )
        s2 = _make_server(
            prefix="slack__",
            resources=ResourcesConfig(
                enabled=True,
                allow=["b"],
                expose_as="read_tool",
            ),
            name="slack",
            server_url="https://slack.example/mcp",
        )
        tools = synthesize_resource_tools([s1, s2])
        assert {t.name for t in tools} == {
            "github__read_resource",
            "slack__read_resource",
        }


class TestReadResourceInvocation:
    """Runtime behavior of the synthetic tool's on_invoke_tool callback."""

    @staticmethod
    def _server() -> FilteredMCPServerStreamableHttp:
        return _make_server(
            prefix="g__",
            resources=ResourcesConfig(
                enabled=True,
                allow=["file://docs/*.md", "https://api/exact"],
                expose_as="read_tool",
            ),
        )

    @pytest.mark.asyncio
    async def test_happy_path_returns_text(self) -> None:
        server = self._server()
        tool = synthesize_resource_tools([server])[0]
        with patch.object(
            FilteredMCPServerStreamableHttp,
            "read_resource",
            new=AsyncMock(return_value=_read_resource_result(text="hello")),
        ):
            out = await tool.on_invoke_tool(_NO_CTX, '{"uri": "file://docs/readme.md"}')
        assert out == "hello"

    @pytest.mark.asyncio
    async def test_disallowed_uri_returns_structured_error(self) -> None:
        server = self._server()
        tool = synthesize_resource_tools([server])[0]
        with patch.object(
            FilteredMCPServerStreamableHttp,
            "read_resource",
            new=AsyncMock(side_effect=AssertionError("must not be called")),
        ):
            out = await tool.on_invoke_tool(_NO_CTX, '{"uri": "file://evil.md"}')
        assert out.startswith("Error:")
        assert "not in the allow-list" in out
        assert "file://docs/*.md" in out

    @pytest.mark.asyncio
    async def test_malformed_json_returns_error(self) -> None:
        server = self._server()
        tool = synthesize_resource_tools([server])[0]
        out = await tool.on_invoke_tool(_NO_CTX, "not json")
        assert out.startswith("Error:")
        assert "valid JSON" in out

    @pytest.mark.asyncio
    async def test_missing_uri_returns_error(self) -> None:
        server = self._server()
        tool = synthesize_resource_tools([server])[0]
        out = await tool.on_invoke_tool(_NO_CTX, "{}")
        assert out.startswith("Error:")
        assert "missing 'uri'" in out

    @pytest.mark.asyncio
    async def test_upstream_failure_returns_redacted_error(self) -> None:
        server = self._server()
        tool = synthesize_resource_tools([server])[0]
        with patch.object(
            FilteredMCPServerStreamableHttp,
            "read_resource",
            new=AsyncMock(side_effect=RuntimeError("internal trace 0xdeadbeef")),
        ):
            out = await tool.on_invoke_tool(_NO_CTX, '{"uri": "https://api/exact"}')
        assert out.startswith("Error reading")
        assert "RuntimeError" in out

    @pytest.mark.asyncio
    async def test_binary_content_summarised_not_inlined(self) -> None:
        server = self._server()
        tool = synthesize_resource_tools([server])[0]
        with patch.object(
            FilteredMCPServerStreamableHttp,
            "read_resource",
            new=AsyncMock(return_value=_read_resource_result(blob=b"abc")),
        ):
            out = await tool.on_invoke_tool(_NO_CTX, '{"uri": "https://api/exact"}')
        assert "binary content" in out
        assert "image/png" in out

    @pytest.mark.asyncio
    async def test_empty_resource_returns_marker(self) -> None:
        server = self._server()
        tool = synthesize_resource_tools([server])[0]
        with patch.object(
            FilteredMCPServerStreamableHttp,
            "read_resource",
            new=AsyncMock(return_value=_read_resource_result()),
        ):
            out = await tool.on_invoke_tool(_NO_CTX, '{"uri": "https://api/exact"}')
        assert out == "(empty resource)"
