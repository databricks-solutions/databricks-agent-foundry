"""Long-term agent memory (``spec.runtime.context.memory``).

Facts about the signed-in user that outlive a chat: preferences, role,
facts about their environment, lessons learned.  The plan, not memory,
holds the state of the current work (see ``docs/explanation/agent-memory.md``).

Follows the semantics of Databricks' reference memory tools
(``databricks_agentkit.openai.memory``): ``_remember`` files one fact under
a topic (shown as ``/<topic>.md``), ``_recall`` searches, ``_forget``
removes a fact and ``_list_memories`` lists the topics.

Facts are kept in the chat-history Lakebase and reached through the chat
UI server's memory routes (:data:`MEMORY_ROUTE`) as the signed-in user,
whose identity the Apps proxy set; it is never a tool argument, so the
model cannot read or write another user's memory.

The tool definitions are the same for every user and stay in the cached
prefix; the per-user topic index goes into the request context, after the
history.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, Final, Generic, TypeVar

import httpx
import mlflow
from agents import FunctionTool
from pydantic import BaseModel, ConfigDict, ValidationError

from payloads.agent.contracts import AgentMemoryRuntime
from payloads.agent.runtime.chat_server import ChatRouteClient, user_route_client
from payloads.agent.runtime.tool_args import invalid_arguments

logger = logging.getLogger(__name__)

MEMORY_ROUTE: Final = "/api/agent/memory"
REMEMBER_TOOL_NAME: Final = "_remember"
RECALL_TOOL_NAME: Final = "_recall"
FORGET_TOOL_NAME: Final = "_forget"
LIST_MEMORIES_TOOL_NAME: Final = "_list_memories"

MEMORY_INDEX_HEADING: Final = "## Memory"
MEMORY_GUIDANCE_HEADING: Final = "## Using memory"
_MAX_TOPIC_CHARS: Final = 64
_NON_SLUG: Final = re.compile(r"[^a-z0-9_-]+")
_SHOWN_PATH: Final = re.compile(r"/([a-z0-9_-]+)\.md")
# The memory routes' answers to a change they refuse; their `detail` is shown to the model.
_REFUSED_STATUSES: Final = frozenset({400, 409})
_UNAVAILABLE: Final = "Error: memory is unavailable right now; continue without it."

_REMEMBER_DESCRIPTION = (
    "Save one durable fact about the user or their environment in memory, filed under a short "
    'topic such as "preferences" or "environment". It lasts across all of the user\'s chats. '
    "Saving a fact that is already stored does nothing. To correct a fact, forget the old one "
    "and remember the new one."
)
_RECALL_DESCRIPTION = "Search the user's memory for facts relevant to a query."
_FORGET_DESCRIPTION = (
    "Remove one fact from memory, when the user asks to forget something or a fact is no "
    "longer true. Pass the fact as stored and its topic, or a path from the memory index."
)
_LIST_DESCRIPTION = (
    "List the memory topics: each path and how many facts it holds. Check it to reuse a "
    "topic instead of inventing a near-duplicate."
)
_GUIDANCE = (
    f"Save facts with `{REMEMBER_TOOL_NAME}` and read them with `{RECALL_TOOL_NAME}`; they last across "
    "the user's chats. When anything is saved, a memory index at the end of these instructions lists "
    "the topics.",
    "- Save stable facts about the user and their environment that help in future chats, such as "
    "preferences, role, ongoing projects or how their data is laid out, when the user states them or "
    "asks you to remember. Progress on the current task belongs in the conversation, not in memory.",
    "- Before asking for something the user may have told you before, check the memory index and "
    f"`{RECALL_TOOL_NAME}`.",
    "- Never save credentials, secrets or sensitive personal data unless the user explicitly asks.",
)


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _FactArgs(_Args):
    fact: str
    topic: str


class _RecallArgs(_Args):
    query: str


class _Reply(BaseModel):
    model_config = ConfigDict(extra="ignore")


class MemoryTopic(_Reply):
    """One of the user's topics and how many facts it holds."""

    topic: str
    facts: int


class _Topics(_Reply):
    topics: list[MemoryTopic]


class _TopicFacts(_Reply):
    facts: list[str]


class _Hit(_Reply):
    topic: str
    fact: str


class _Hits(_Reply):
    hits: list[_Hit]


class _Saved(_Reply):
    status: str


def topic_slug(topic: str) -> str:
    """The stored topic for a model-supplied one: lower-case letters, digits, ``-`` and ``_``."""
    ascii_topic = unicodedata.normalize("NFKD", topic).encode("ascii", "ignore").decode()
    slug = _NON_SLUG.sub("-", ascii_topic.lower()).strip("-_")
    return slug[:_MAX_TOPIC_CHARS].rstrip("-_") or "general"


def forget_slug(topic: str) -> str | None:
    """A topic, or a path shown in the index, as a stored topic; ``None`` for any other path."""
    topic = topic.strip()
    if not topic.startswith("/"):
        return topic_slug(topic)
    shown = _SHOWN_PATH.fullmatch(topic)
    return shown.group(1) if shown else None


def topic_path(slug: str) -> str:
    """How a topic is shown to the model."""
    return f"/{slug}.md"


def _topic_line(topic: MemoryTopic) -> str:
    return f"{topic_path(topic.topic)}: {topic.facts} fact{'' if topic.facts == 1 else 's'}"


def _refusal(response: httpx.Response) -> str:
    try:
        detail = response.json().get("detail")
    except ValueError:
        detail = None
    return f"Error: {detail}" if isinstance(detail, str) else _UNAVAILABLE


async def _topics(client: ChatRouteClient) -> list[MemoryTopic]:
    return _Topics.model_validate_json(await client.request("GET", "topics")).topics


ArgsT = TypeVar("ArgsT", bound=BaseModel)


class _MemoryTool(Generic[ArgsT]):
    """Parses the arguments, calls the memory routes, maps failures to text."""

    args_model: ClassVar[type[BaseModel]]

    def __init__(self, client: ChatRouteClient) -> None:
        self._client = client

    async def __call__(self, _ctx: Any, args_json: str) -> str:
        try:
            args = self.args_model.model_validate_json(args_json or "{}")
        except ValidationError as exc:
            return invalid_arguments(exc)
        try:
            return await self._run(args)  # type: ignore[arg-type]
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in _REFUSED_STATUSES:
                return _refusal(exc.response)
            logger.warning("Memory tool %s failed: %s", type(self).__name__, exc)
        except (httpx.HTTPError, ValidationError) as exc:
            logger.warning("Memory tool %s failed: %s", type(self).__name__, exc)
        return _UNAVAILABLE

    async def _run(self, args: ArgsT) -> str:
        raise NotImplementedError


class Remember(_MemoryTool[_FactArgs]):
    args_model = _FactArgs

    async def _run(self, args: _FactArgs) -> str:
        slug = topic_slug(args.topic)
        body = {"topic": slug, "fact": args.fact}
        saved = _Saved.model_validate_json(await self._client.request("POST", "remember", body))
        if saved.status == "exists":
            return f"Already remembered in {topic_path(slug)}."
        return f"Remembered in {topic_path(slug)}."


class Recall(_MemoryTool[_RecallArgs]):
    args_model = _RecallArgs

    async def _run(self, args: _RecallArgs) -> str:
        found = _Hits.model_validate_json(await self._client.request("POST", "search", {"query": args.query}))
        by_topic: dict[str, list[str]] = {}
        for hit in found.hits:
            by_topic.setdefault(hit.topic, []).append(hit.fact)
        lines: list[str] = []
        for slug, facts in by_topic.items():
            lines.append(f"{topic_path(slug)}:")
            lines.extend(f"  {fact}" for fact in facts)
        return "\n".join(lines) or "No relevant memories."


class Forget(_MemoryTool[_FactArgs]):
    args_model = _FactArgs

    async def _run(self, args: _FactArgs) -> str:
        slug = forget_slug(args.topic)
        if slug is None:
            return f"Error: invalid memory path {args.topic.strip()!r}; use a topic or a listed path."
        try:
            await self._client.request("POST", "forget", {"topic": slug, "fact": args.fact})
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise
            return await self._not_found(slug)
        return f"Forgot it in {topic_path(slug)}."

    async def _not_found(self, slug: str) -> str:
        stored = _TopicFacts.model_validate_json(await self._client.request("GET", f"topics/{slug}"))
        if not stored.facts:
            return f"No memories in {topic_path(slug)}."
        shown = "".join(f"\n  {fact}" for fact in stored.facts)
        return f"Not found in {topic_path(slug)}.{shown}"


class ListMemories(_MemoryTool[_Args]):
    args_model = _Args

    async def _run(self, args: _Args) -> str:
        return "\n".join(_topic_line(topic) for topic in await _topics(self._client)) or "No memories yet."


def _params(properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def _fact_params(fact_help: str) -> dict[str, Any]:
    return _params(
        {
            "fact": {"type": "string", "description": fact_help},
            "topic": {"type": "string", "description": "A short topic, or a path from the memory index."},
        }
    )


def memory_function_tools(client: ChatRouteClient) -> tuple[FunctionTool, ...]:
    """The four memory tools; their definitions are fixed."""
    query = {"query": {"type": "string", "description": "What to look for, in natural language."}}
    specs: Sequence[tuple[str, str, dict[str, Any], _MemoryTool[Any]]] = (
        (
            REMEMBER_TOOL_NAME,
            _REMEMBER_DESCRIPTION,
            _fact_params('One self-contained fact, such as "Prefers email over phone".'),
            Remember(client),
        ),
        (RECALL_TOOL_NAME, _RECALL_DESCRIPTION, _params(query), Recall(client)),
        (FORGET_TOOL_NAME, _FORGET_DESCRIPTION, _fact_params("The fact to remove, as stored."), Forget(client)),
        (LIST_MEMORIES_TOOL_NAME, _LIST_DESCRIPTION, _params({}), ListMemories(client)),
    )
    return tuple(
        FunctionTool(
            name=name,
            description=description,
            params_json_schema=params,
            on_invoke_tool=handler,
            strict_json_schema=False,
        )
        for name, description, params, handler in specs
    )


def render_memory_index(topics: Sequence[MemoryTopic], max_entries: int) -> str:
    """The topic index for the request context; empty when nothing is stored."""
    if not topics:
        return ""
    lines = [
        MEMORY_INDEX_HEADING,
        f"Topics saved with `{REMEMBER_TOOL_NAME}`; read them with `{RECALL_TOOL_NAME}`.",
        *(f"- {_topic_line(topic)}" for topic in topics[:max_entries]),
    ]
    if len(topics) > max_entries:
        lines.append(f"- ...and {len(topics) - max_entries} more (`{LIST_MEMORIES_TOOL_NAME}`).")
    return "\n".join(lines)


def render_memory_guidance() -> str:
    """How to use memory, for the cached prefix."""
    return "\n".join([MEMORY_GUIDANCE_HEADING, *_GUIDANCE])


@dataclass(frozen=True)
class AgentMemory:
    """This request's memory tools, usage guidance and topic index."""

    tools: tuple[FunctionTool, ...]
    guidance: str
    index: str


async def _load_topics(client: ChatRouteClient) -> list[MemoryTopic]:
    """The user's topics; none (logged) when the memory cannot be read."""
    with mlflow.start_span(name="memory.prepare", span_type="TASK") as span:
        try:
            topics = await _topics(client)
        except (httpx.HTTPError, ValidationError) as exc:
            logger.warning("Memory unavailable for this request: %s", exc)
            return []
        span.set_attribute("memory.topics", len(topics))
    return topics


async def prepare_memory(settings: AgentMemoryRuntime, forwarded_headers: Mapping[str, str]) -> AgentMemory:
    """This request's memory tools and the signed-in user's topic index."""
    client = user_route_client(MEMORY_ROUTE, forwarded_headers)
    return AgentMemory(
        tools=memory_function_tools(client),
        guidance=render_memory_guidance(),
        index=render_memory_index(await _load_topics(client), settings.index_max_entries),
    )
