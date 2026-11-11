"""Tests for the Tier-3 code-tool contract (decorator, schema, loader).

The loader imports a real module written under a temp instance tree, then the
discovered tool is driven end-to-end with an injected ``HttpClient`` over an
``httpx.MockTransport`` -- proving the escape hatch works and stays bound to
the payload's rails.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from payloads.rest_mcp.contracts import RestConfig
from payloads.rest_mcp.runtime.code_tools import discover_code_tools, tool
from payloads.rest_mcp.runtime.http_client import HttpClient

_SCORE_MODULE = """
from payloads.rest_mcp.runtime import tool, HttpClient


@tool(name="score_image", description="Classify an image by URL.")
async def score_image(image_url: str, *, http: HttpClient) -> dict:
    data = await http.fetch_bytes(image_url)
    return await http.post_json("/invocations", {"size": len(data)})
"""


def _write_module(root: Path, rel: str, body: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _rest() -> RestConfig:
    return RestConfig.model_validate(
        {
            "base_url": "https://api.example.com",
            "allowed_hosts": ["api.example.com"],
            "auth": {"mode": "none"},
        }
    )


class TestToolDecorator:
    def test_sync_function_rejected(self) -> None:
        def not_async(x: str) -> None:
            return None

        with pytest.raises(TypeError, match="async"):
            tool(name="bad", description="d")(not_async)  # type: ignore[arg-type]

    def test_schema_derived_from_signature(self) -> None:
        @tool(name="t", description="d")
        async def fn(a: str, b: int = 3, *, http: HttpClient) -> dict[str, object]:
            return {}

        meta = getattr(fn, "__rest_mcp_code_tool__")
        assert meta.input_schema["properties"]["a"] == {"type": "string"}
        assert meta.input_schema["properties"]["b"] == {"type": "integer"}
        assert "http" not in meta.input_schema["properties"]
        assert meta.input_schema["required"] == ["a"]
        assert meta.input_schema["additionalProperties"] is False

    def test_explicit_input_schema_wins(self) -> None:
        explicit = {"type": "object", "properties": {"q": {"type": "string"}}}

        @tool(name="t", description="d", input_schema=explicit)
        async def fn(q: str, *, http: HttpClient) -> dict[str, object]:
            return {}

        assert getattr(fn, "__rest_mcp_code_tool__").input_schema == explicit


class TestDiscovery:
    def test_discovers_decorated_tool(self, tmp_path: Path) -> None:
        _write_module(tmp_path, "payloads/rest_mcp/tools/score.py", _SCORE_MODULE)
        tools = discover_code_tools(tmp_path, "payloads/rest_mcp/tools/score.py")
        assert len(tools) == 1
        assert tools[0].name == "score_image"
        assert tools[0].description == "Classify an image by URL."
        assert tools[0].input_schema["required"] == ["image_url"]

    @pytest.mark.asyncio
    async def test_discovered_tool_runs_through_rails(self, tmp_path: Path) -> None:
        _write_module(tmp_path, "tools/score.py", _SCORE_MODULE)
        (code_tool,) = discover_code_tools(tmp_path, "tools/score.py")

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(200, content=b"1234")
            return httpx.Response(200, json={"echoed_size": 4})

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            http = HttpClient(_rest(), token=None, client=client)
            result = await code_tool.func(image_url="https://api.example.com/i.png", http=http)

        assert result == {"echoed_size": 4}

    def test_module_escaping_instance_tree_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="escapes"):
            discover_code_tools(tmp_path, "../evil.py")

    def test_missing_module_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="existing .py"):
            discover_code_tools(tmp_path, "tools/absent.py")

    def test_non_python_module_rejected(self, tmp_path: Path) -> None:
        _write_module(tmp_path, "tools/x.txt", "nope")
        with pytest.raises(ValueError, match="existing .py"):
            discover_code_tools(tmp_path, "tools/x.txt")

    def test_duplicate_tool_names_rejected(self, tmp_path: Path) -> None:
        dupe = (
            "from payloads.rest_mcp.runtime import tool, HttpClient\n"
            "@tool(name='dup', description='a')\n"
            "async def one(*, http: HttpClient) -> dict:\n    return {}\n"
            "@tool(name='dup', description='b')\n"
            "async def two(*, http: HttpClient) -> dict:\n    return {}\n"
        )
        _write_module(tmp_path, "tools/dupe.py", dupe)
        with pytest.raises(ValueError, match="duplicate"):
            discover_code_tools(tmp_path, "tools/dupe.py")
