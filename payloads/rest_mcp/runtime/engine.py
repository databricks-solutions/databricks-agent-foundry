"""Pure REST->MCP translation engine for the ``rest_mcp`` payload.

This is a **pure library**: it imports no FastAPI, MCP, or Databricks SDK, so
the REST->MCP mapping is unit-testable in isolation and the server layer
(a later slice) stays thin.  It covers the declarative tiers:

* **Tier 1 (flat args):** path / query / flat-body params derived from ``args``.
* **Tier 2 (templated body):** a JSON ``body_template`` with ``{{arg}}``
  placeholders substituted from the call arguments.

Two derived artifacts are generated from the same tool declaration so they
cannot drift: the MCP ``inputSchema`` (:func:`build_tool_specs`) and the
``httpx`` request (:func:`prepare_request`).  The response projection +
byte-cap live in :func:`shape_response`.

Auth material (a resolved secret bearer token or a forwarded OBO token) is
passed *in* by the caller -- the engine never fetches secrets and never logs
tokens.  The upstream host is re-checked against ``allowed_hosts`` on every
call (defence in depth on top of the contract-time validation).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from re import Match
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from payloads.rest_mcp.contracts import (
    _TEMPLATE_PLACEHOLDER,
    NoAuth,
    OboAuth,
    RestConfig,
    StaticAuth,
    ToolSpec,
)

# Truncation marker appended when a response is trimmed to ``max_response_bytes``.
_TRUNCATED_MARKER = "...[truncated]"


class ToolInputError(ValueError):
    """A tool was called with missing / invalid arguments (a client error)."""


class EgressError(RuntimeError):
    """The computed upstream URL is not permitted by ``allowed_hosts`` / https."""


class UpstreamError(RuntimeError):
    """The upstream REST API returned a 4xx / 5xx response.

    Carries the status code and a truncated, token-free detail so the server
    layer can surface a structured MCP error.
    """

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"upstream returned {status_code}: {detail}")


@dataclass(frozen=True)
class ToolDescriptor:
    """The MCP-card view of a tool: what a client sees before calling it."""

    name: str
    description: str
    input_schema: dict[str, Any]


def build_input_schema(tool: ToolSpec) -> dict[str, Any]:
    """Derive the MCP ``inputSchema`` for a tool.

    Returns the author's ``input_schema`` verbatim when given (Tier 2 shaped
    bodies); otherwise derives an object schema from the declared ``args`` so
    the card, the request, and the projection all stem from one declaration.
    """
    if tool.request.input_schema is not None:
        return dict(tool.request.input_schema)

    properties: dict[str, Any] = {}
    required: list[str] = []
    for arg in tool.request.args:
        prop: dict[str, Any] = {"type": arg.type}
        if arg.description is not None:
            prop["description"] = arg.description
        properties[arg.name] = prop
        if arg.required:
            required.append(arg.name)

    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return schema


def build_tool_specs(tools: list[ToolSpec]) -> list[ToolDescriptor]:
    """Build the MCP descriptors for a list of declarative tools."""
    return [
        ToolDescriptor(
            name=tool.name,
            description=tool.description,
            input_schema=build_input_schema(tool),
        )
        for tool in tools
    ]


# Sent on every upstream request unless the caller already set one. Some APIs
# (e.g. GitHub) reject requests without a User-Agent; a non-empty default keeps
# the proxy working out of the box while staying overridable via ``setdefault``.
DEFAULT_USER_AGENT = "agent-foundry-rest-mcp"


def build_auth_header(rest: RestConfig, token: str | None) -> dict[str, str]:
    """Compute the hop-2 auth header without ever logging the token.

    Shared rail: the code-tool ``HttpClient`` reuses this so a bespoke tool
    gets exactly the same auth handling as a declarative one.
    """
    auth = rest.auth
    if isinstance(auth, NoAuth):
        return {}
    if token is None:
        raise ToolInputError(f"auth mode {auth.mode!r} requires a token but none was provided")
    if isinstance(auth, StaticAuth):
        return {auth.header: f"{auth.value_prefix}{token}"}
    if isinstance(auth, OboAuth):
        return {"Authorization": f"Bearer {token}"}
    raise ToolInputError(f"unsupported auth mode {auth.mode!r}")  # pragma: no cover - exhaustive


def _render_path(tool: ToolSpec, arguments: Mapping[str, Any]) -> str:
    """Substitute ``{name}`` path segments with URL-encoded argument values."""
    path = tool.request.path
    for arg in tool.request.args:
        if arg.location != "path":
            continue
        if arg.name not in arguments:
            raise ToolInputError(f"missing required path argument {arg.name!r}")
        path = path.replace(f"{{{arg.name}}}", quote(str(arguments[arg.name]), safe=""))
    return path


def _query_params(tool: ToolSpec, arguments: Mapping[str, Any]) -> dict[str, Any]:
    return {
        arg.name: arguments[arg.name] for arg in tool.request.args if arg.location == "query" and arg.name in arguments
    }


def _render_body(tool: ToolSpec, arguments: Mapping[str, Any]) -> Any | None:
    """Build the JSON body from a ``body_template`` (Tier 2) or flat body args."""
    template = tool.request.body_template
    if template is not None:
        return _render_body_template(template, arguments)

    body_args = [arg for arg in tool.request.args if arg.location == "body"]
    if not body_args:
        return None
    for arg in body_args:
        if arg.required and arg.name not in arguments:
            raise ToolInputError(f"missing required body argument {arg.name!r}")
    return {arg.name: arguments[arg.name] for arg in body_args if arg.name in arguments}


def _render_body_template(template: str, arguments: Mapping[str, Any]) -> Any:
    """Substitute ``{{arg}}`` placeholders with JSON-encoded values, then parse.

    Values are JSON-encoded so the rendered document is well-formed regardless
    of type; parsing the result validates it and yields a native body for
    ``httpx`` to re-serialise.
    """

    def _replace(match: Match[str]) -> str:
        name = match.group(1)
        if name not in arguments:
            raise ToolInputError(f"missing argument {name!r} for body_template")
        return json.dumps(arguments[name])

    rendered = _TEMPLATE_PLACEHOLDER.sub(_replace, template)
    try:
        return json.loads(rendered)
    except json.JSONDecodeError as exc:
        raise ToolInputError(f"body_template did not render to valid JSON: {exc}") from None


def _require_declared_args(tool: ToolSpec, arguments: Mapping[str, Any]) -> None:
    for arg in tool.request.args:
        if arg.required and arg.name not in arguments:
            raise ToolInputError(f"missing required argument {arg.name!r}")


def guard_egress(rest: RestConfig, url: str) -> None:
    """Fail-closed egress rail: https-only + host in ``allowed_hosts``.

    Shared by the declarative engine and the code-tool ``HttpClient`` so both
    honour the exact same allow-list.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise EgressError(f"refusing non-https egress to {url!r}")
    if parsed.hostname not in rest.allowed_hosts:
        raise EgressError(f"host {parsed.hostname!r} is not in allowed_hosts {rest.allowed_hosts!r}")


def prepare_request(
    rest: RestConfig,
    tool: ToolSpec,
    arguments: Mapping[str, Any],
    *,
    token: str | None,
) -> httpx.Request:
    """Build the upstream ``httpx.Request`` for a tool call, fail-closed.

    Enforces required arguments, substitutes path / query / body values,
    applies the hop-2 auth header, and re-checks the target against
    ``allowed_hosts`` (https-only).
    """
    _require_declared_args(tool, arguments)
    path = _render_path(tool, arguments)
    url = f"{rest.base_url.rstrip('/')}{path}"
    guard_egress(rest, url)

    params = _query_params(tool, arguments)
    headers = build_auth_header(rest, token)
    headers.setdefault("User-Agent", DEFAULT_USER_AGENT)
    # A body is only meaningful for non-GET methods; GET stays side-effect free.
    json_body = None if tool.request.method == "GET" else _render_body(tool, arguments)

    return httpx.Request(
        tool.request.method,
        url,
        params=params or None,
        json=json_body,
        headers=headers,
    )


def shape_response(rest: RestConfig, tool: ToolSpec, response: httpx.Response) -> str:
    """Map an upstream response to the text an MCP tool returns.

    Raises :class:`UpstreamError` on 4xx / 5xx; otherwise applies the response
    projection (when set) and the ``max_response_bytes`` cap.
    """
    cap = rest.defaults.max_response_bytes
    if response.status_code >= 400:
        raise UpstreamError(response.status_code, cap_text(response.text, cap))

    return cap_text(_project(tool, response), cap)


def _project(tool: ToolSpec, response: httpx.Response) -> str:
    """Return the projected JSON (when a projection is set) else the raw text."""
    projection = tool.response.project
    if projection is None:
        return response.text
    try:
        parsed = response.json()
    except (json.JSONDecodeError, ValueError):
        # Projection assumes a JSON object; if the upstream didn't return one,
        # fall back to the raw text rather than failing the call.
        return response.text
    if not isinstance(parsed, dict):
        return response.text
    projected = {key: parsed[key] for key in projection if key in parsed}
    return json.dumps(projected)


def cap_text(text: str, max_bytes: int) -> str:
    """Trim ``text`` to at most ``max_bytes`` UTF-8 bytes, marking truncation.

    Shared rail: the code-tool ``HttpClient`` reuses this so bespoke tools are
    bounded by the same ``max_response_bytes`` cap.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    trimmed = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return f"{trimmed}{_TRUNCATED_MARKER}"


async def call_tool(
    rest: RestConfig,
    tool: ToolSpec,
    arguments: Mapping[str, Any],
    *,
    token: str | None,
    client: httpx.AsyncClient,
) -> str:
    """Execute one declarative tool call end-to-end and return MCP text.

    The ``client`` is injected so the transport (timeouts, mounts, or a test
    ``MockTransport``) is owned by the caller.
    """
    request = prepare_request(rest, tool, arguments, token=token)
    response = await client.send(request)
    return shape_response(rest, tool, response)


__all__ = [
    "EgressError",
    "ToolDescriptor",
    "ToolInputError",
    "UpstreamError",
    "build_auth_header",
    "build_input_schema",
    "build_tool_specs",
    "call_tool",
    "cap_text",
    "guard_egress",
    "prepare_request",
    "shape_response",
]
