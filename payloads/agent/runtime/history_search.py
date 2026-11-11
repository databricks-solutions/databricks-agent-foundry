"""Searchable and readable history after compaction (#685): ``_search_history`` and ``_read_history``.

The chat UI replaces the older turns of a long chat with a summary for the
model; the turns themselves stay in Lakebase.  The tools ask the chat UI
server, on localhost, to search or page through the summarized turns of
this chat (``POST /api/chat/:id/history/search`` and ``.../read``), as the
user, through :mod:`payloads.agent.runtime.chat_server`.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, ClassVar, Final, Generic, TypeVar

import httpx
from agents import FunctionTool
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agent_foundry import env
from payloads.agent.runtime.chat_server import ChatRouteClient, chat_route_client

logger = logging.getLogger(__name__)

HISTORY_ROUTE: Final = "/api/chat/{chat_id}/history"
HISTORY_SEARCH_ROUTE: Final = f"{HISTORY_ROUTE}/search"
HISTORY_READ_ROUTE: Final = f"{HISTORY_ROUTE}/read"
SEARCH_HISTORY_TOOL_NAME: Final = "_search_history"
READ_HISTORY_TOOL_NAME: Final = "_read_history"
MAX_QUERY_CHARS: Final = 500
_UNAVAILABLE: Final = "Error: the chat history is unavailable right now; continue without it."
_NOTHING_SUMMARIZED: Final = "Nothing in this chat is summarized yet."
_NO_MATCH: Final = "No match: nothing in this chat is summarized yet, or no summarized turn has these words."

_GUIDANCE: Final = (
    "## Earlier turns\n"
    "- In a long chat the older turns are replaced by a summary. When you need an exact detail it "
    f"leaves out, such as a value, a name or a tool result, find it with `{SEARCH_HISTORY_TOOL_NAME}` "
    "before asking the user again or calling the tool again. Read around a hit, or browse the turns "
    f"when you don't know what to search for, with `{READ_HISTORY_TOOL_NAME}`. Never repeat a tool "
    "call that changes something just to get its result back."
)
_SEARCH_DESCRIPTION: Final = (
    "Search the turns of this chat that a summary replaced, full tool results included. Returns up to "
    "5 excerpts with their turn and offset, best match first; read around one with "
    f"`{READ_HISTORY_TOOL_NAME}(turn, offset)`. Use distinctive words such as names, ids or numbers."
)
_READ_DESCRIPTION: Final = (
    "Read the turns of this chat that a summary replaced. Without a turn: an outline of those turns, "
    "with what the user asked and each tool call with its offset. With a turn: one page of it from "
    "offset (default 0), full tool results included; the reply says where the next page starts."
)
_SEARCH_PARAMS: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {"query": {"type": "string", "minLength": 1, "maxLength": MAX_QUERY_CHARS}},
    "required": ["query"],
    "additionalProperties": False,
}
_READ_PARAMS: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "turn": {"type": "integer", "minimum": 1, "description": "The turn to read; omit for the outline."},
        "offset": {"type": "integer", "minimum": 0, "description": "Where in the turn to start, in characters."},
    },
    "additionalProperties": False,
}


class _SearchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=MAX_QUERY_CHARS)


class _ReadArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    turn: int | None = Field(default=None, ge=1)
    offset: int = Field(default=0, ge=0)


class _Hit(BaseModel):
    turn: int
    at: int
    excerpt: str


class _SearchResponse(BaseModel):
    hits: list[_Hit]


class _ToolCallOutline(BaseModel):
    label: str
    at: int
    chars: int


class _TurnOutline(BaseModel):
    turn: int
    request: str
    tool_calls: list[_ToolCallOutline] = Field(alias="toolCalls")
    more_tool_calls: int = Field(default=0, alias="moreToolCalls")
    chars: int


class _Page(BaseModel):
    turn: int
    turns: int
    offset: int
    end: int
    chars: int
    text: str


class _ReadResponse(BaseModel):
    outline: list[_TurnOutline] | None = None
    page: _Page | None = None
    turns: int = 0


def _render_hits(hits: list[_Hit]) -> str:
    if not hits:
        return _NO_MATCH
    return "\n\n".join(f"Turn {hit.turn} at {hit.at}: {hit.excerpt}" for hit in hits)


def _render_turn_outline(turn: _TurnOutline) -> list[str]:
    head = f"Turn {turn.turn}: {turn.request or '(no user message)'} ({turn.chars} characters)"
    calls = [f"  at {call.at}: {call.label} ({call.chars} characters)" for call in turn.tool_calls]
    more = [f"  … and {turn.more_tool_calls} more tool calls"] if turn.more_tool_calls else []
    return [head, *calls, *more]


def _render_outline(outline: list[_TurnOutline]) -> str:
    if not outline:
        return _NOTHING_SUMMARIZED
    header = f"Turns the summary replaced; read from a tool call with `{READ_HISTORY_TOOL_NAME}(turn, offset)`:"
    return "\n".join([header, *(line for turn in outline for line in _render_turn_outline(turn))])


def _render_page(page: _Page) -> str:
    header = f"Turn {page.turn} of {page.turns}, characters {page.offset}-{page.end} of {page.chars}:"
    if page.end < page.chars:
        footer = f"More: `{READ_HISTORY_TOOL_NAME}(turn={page.turn}, offset={page.end})`."
    else:
        footer = f"End of turn {page.turn}."
    return f"{header}\n{page.text}\n\n{footer}"


def _render_read(turn: int, response: _ReadResponse) -> str:
    if response.outline is not None:
        return _render_outline(response.outline)
    if response.page is not None:
        return _render_page(response.page)
    if response.turns == 0:
        return _NOTHING_SUMMARIZED
    return f"No summarized turn {turn}; the summary replaced turns 1 to {response.turns}."


_ArgsT = TypeVar("_ArgsT", bound=BaseModel)


class _HistoryTool(Generic[_ArgsT]):
    """Parses the arguments, calls the chat UI server, maps failures to text."""

    args_model: type[_ArgsT]
    # Shown to the model when the arguments are invalid.
    invalid_args: ClassVar[str]

    def __init__(self, client: ChatRouteClient) -> None:
        self.client = client

    async def __call__(self, _ctx: Any, args_json: str) -> str:
        try:
            args = self.args_model.model_validate_json(args_json or "{}")
        except ValidationError:
            return self.invalid_args
        try:
            return await self._run(args)
        except (httpx.HTTPError, ValidationError) as exc:
            logger.warning("%s failed: %s", type(self).__name__, exc)
            return _UNAVAILABLE

    async def _run(self, args: _ArgsT) -> str:
        raise NotImplementedError


class SearchHistory(_HistoryTool[_SearchArgs]):
    args_model = _SearchArgs
    invalid_args = f"Error: give a query of 1 to {MAX_QUERY_CHARS} characters."

    async def _run(self, args: _SearchArgs) -> str:
        content = await self.client.request("POST", "search", {"query": args.query})
        return _render_hits(_SearchResponse.model_validate_json(content).hits)


class ReadHistory(_HistoryTool[_ReadArgs]):
    args_model = _ReadArgs
    invalid_args = "Error: turn must be a whole number from 1, and offset one from 0."

    async def _run(self, args: _ReadArgs) -> str:
        body: dict[str, Any] = {} if args.turn is None else {"turn": args.turn, "offset": args.offset}
        content = await self.client.request("POST", "read", body)
        return _render_read(args.turn or 0, _ReadResponse.model_validate_json(content))


@dataclass(frozen=True)
class AgentHistorySearch:
    """This request's history tools and their guidance."""

    tools: tuple[FunctionTool, ...]
    guidance: str


def prepare_history_search(
    conversation_id: str | None, forwarded_headers: Mapping[str, str]
) -> AgentHistorySearch | None:
    """The tools and guidance while compaction is on, for a request with a usable conversation id."""
    if not env.read_bool(env.ENABLE_COMPACTION):
        return None
    client = chat_route_client(HISTORY_ROUTE, conversation_id, forwarded_headers)
    if client is None:
        return None
    tools = (
        FunctionTool(
            name=SEARCH_HISTORY_TOOL_NAME,
            description=_SEARCH_DESCRIPTION,
            params_json_schema=_SEARCH_PARAMS,
            on_invoke_tool=SearchHistory(client),
            strict_json_schema=False,
        ),
        FunctionTool(
            name=READ_HISTORY_TOOL_NAME,
            description=_READ_DESCRIPTION,
            params_json_schema=_READ_PARAMS,
            on_invoke_tool=ReadHistory(client),
            strict_json_schema=False,
        ),
    )
    return AgentHistorySearch(tools=tools, guidance=_GUIDANCE)
