"""Tier-3 code tools: the instance-side escape hatch, bound by a contract.

When a REST shape needs *computation* (base64-encode an image, derive a field,
custom batching) that declarative config can't express, an instance drops in a
Python module implementing this contract::

    # instances/<name>/payloads/rest_mcp/tools/encode_and_score.py
    from rest_mcp import tool, HttpClient   # payload-owned contract

    @tool(name="score_image", description="Classify an image by URL.")
    async def score_image(image_url: str, *, http: HttpClient) -> dict:
        img = await http.fetch_bytes(image_url)        # rails enforced
        return await http.post_json("/invocations", {"inputs": [b64(img)]})

The contract keeps the escape hatch honest:

* A tool is an **async** function decorated with :func:`tool`; its non-``http``
  parameters become the MCP ``inputSchema`` (derived from type hints, or given
  explicitly).
* The only sanctioned egress is the injected :class:`HttpClient`, which is
  pre-bound to the API's auth + ``allowed_hosts`` + byte cap.  Security review
  is bounded to "what does the body do with the injected client".
* :func:`discover_code_tools` loads a module strictly from *within* the
  instance tree (the module path is also validated at contract time), so a code
  tool can never point outside the deployed instance.

Registering the discovered tools onto an API's MCP server is the host-app
slice's job; this module stays FastMCP-free and unit-testable.
"""

from __future__ import annotations

import importlib.util
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, get_origin

# Attribute stamped on a decorated function so the loader can find it.
_TOOL_MARKER = "__rest_mcp_code_tool__"

# Python annotation -> JSON Schema type for the derived ``inputSchema``.
_PY_TO_JSON_TYPE: dict[type, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}

CodeToolFunc = Callable[..., Awaitable[Any]]


@dataclass(frozen=True)
class _ToolMeta:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class CodeTool:
    """A discovered Tier-3 tool: its MCP card view plus the callable body."""

    name: str
    description: str
    input_schema: dict[str, Any]
    func: CodeToolFunc


def tool(
    *,
    name: str,
    description: str,
    input_schema: dict[str, Any] | None = None,
) -> Callable[[CodeToolFunc], CodeToolFunc]:
    """Mark an async function as a Tier-3 code tool.

    ``input_schema`` is optional: when omitted it is derived from the
    function's non-``http`` parameters (type hints + whether they have a
    default).  The function is returned unchanged so it stays directly
    callable / testable.
    """

    def decorator(func: CodeToolFunc) -> CodeToolFunc:
        if not inspect.iscoroutinefunction(func):
            raise TypeError(f"code tool {name!r} must be an async function")
        schema = input_schema if input_schema is not None else _derive_input_schema(func)
        setattr(func, _TOOL_MARKER, _ToolMeta(name=name, description=description, input_schema=schema))
        return func

    return decorator


def _derive_input_schema(func: CodeToolFunc) -> dict[str, Any]:
    """Build an object schema from a tool's parameters, excluding ``http``.

    Annotations are resolved with ``eval_str=True`` so string annotations (the
    ``from __future__ import annotations`` case, common in instance modules)
    map to real types.  ``list[str]`` and friends map via their origin.
    """
    hints = _resolved_hints(func)
    properties: dict[str, Any] = {}
    required: list[str] = []
    for param_name, param in inspect.signature(func).parameters.items():
        if param_name == "http":
            continue
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        prop: dict[str, Any] = {}
        json_type = _json_type_for(hints.get(param_name))
        if json_type is not None:
            prop["type"] = json_type
        properties[param_name] = prop
        if param.default is inspect.Parameter.empty:
            required.append(param_name)

    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return schema


def _resolved_hints(func: CodeToolFunc) -> dict[str, Any]:
    try:
        return inspect.get_annotations(func, eval_str=True)
    except (NameError, TypeError):
        # An annotation that can't be resolved (an exotic forward ref) just
        # yields an untyped property rather than failing the whole tool.
        return {}


def _json_type_for(annotation: Any) -> str | None:
    key = annotation if isinstance(annotation, type) else get_origin(annotation)
    if isinstance(key, type):
        return _PY_TO_JSON_TYPE.get(key)
    return None


def discover_code_tools(instance_root: Path, module_rel_path: str) -> list[CodeTool]:
    """Import an instance code-tool module and collect its decorated tools.

    ``module_rel_path`` is resolved *within* ``instance_root``; a path that
    escapes the instance tree, is absent, or is not a ``.py`` file is rejected
    (defence in depth on top of the contract-time validation).
    """
    root = instance_root.resolve()
    module_path = (root / module_rel_path).resolve()
    if not module_path.is_relative_to(root):
        raise ValueError(f"code tool module {module_rel_path!r} escapes the instance tree")
    if module_path.suffix != ".py" or not module_path.is_file():
        raise ValueError(f"code tool module {module_rel_path!r} is not an existing .py file")

    module = _import_module_from_path(module_path)
    tools = [
        CodeTool(
            name=meta.name,
            description=meta.description,
            input_schema=meta.input_schema,
            func=member,
        )
        for member in vars(module).values()
        if callable(member) and (meta := getattr(member, _TOOL_MARKER, None)) is not None
    ]
    _reject_duplicate_names(module_rel_path, tools)
    return tools


def _import_module_from_path(module_path: Path) -> Any:
    # A stable, collision-free module name derived from the file location.
    module_name = f"rest_mcp_code_tool_{module_path.stem}_{abs(hash(module_path)) & 0xFFFFFF}"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot import code tool module {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _reject_duplicate_names(module_rel_path: str, tools: list[CodeTool]) -> None:
    names = [t.name for t in tools]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise ValueError(f"code tool module {module_rel_path!r} declares duplicate tool names: {dupes}")


__all__ = ["CodeTool", "discover_code_tools", "tool"]
