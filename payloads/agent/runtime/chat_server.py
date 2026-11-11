"""Calls from the agent to the chat UI server's routes, on localhost, as the user.

The chat UI server keeps the chat-history Lakebase; the agent opens no
database connection.  It forwards the user's identity headers, which the
Apps proxy set on the request, so the routes' own access checks apply.
The chat is the request's conversation id and the URLs are built here,
never taken from tool arguments.  Used by the history tools, the plan and
long-term memory.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Literal

from agent_foundry import env
from payloads.agent.runtime.start_server import Latin1HeaderClient

logger = logging.getLogger(__name__)

# The headers the chat UI server builds its session from.
IDENTITY_HEADERS: Final = ("x-forwarded-user", "x-forwarded-email", "x-forwarded-preferred-username")

_TIMEOUT_SECONDS: Final = 10.0
# One path segment: no dots or slashes can reach the URL.
_CHAT_ID_PATTERN: Final = re.compile(r"[A-Za-z0-9_-]{1,128}")

Method = Literal["GET", "POST", "PUT", "DELETE"]


@dataclass(frozen=True)
class ChatRouteClient:
    """One route of this chat on the chat UI server, called as the user."""

    base_url: str
    headers: Mapping[str, str] = field(repr=False)

    async def request(self, method: Method, action: str = "", body: dict[str, Any] | None = None) -> bytes:
        """The response body; raises ``httpx.HTTPError`` on a failed call or an error status."""
        url = f"{self.base_url}/{action}" if action else self.base_url
        # trust_env=False: the user's headers go to localhost only, never via a configured proxy.
        async with Latin1HeaderClient(timeout=_TIMEOUT_SECONDS, trust_env=False) as client:
            response = await client.request(method, url, json=body, headers=dict(self.headers))
        response.raise_for_status()
        return response.content


def chat_route_client(
    route: str, conversation_id: str | None, forwarded_headers: Mapping[str, str]
) -> ChatRouteClient | None:
    """A client for *route* (with a ``{chat_id}`` field) of this chat; ``None`` without a plain chat id."""
    if conversation_id is None:
        return None
    if not _CHAT_ID_PATTERN.fullmatch(conversation_id):
        logger.warning("Chat routes off: conversation id is not a plain path segment")
        return None
    return user_route_client(route.format(chat_id=conversation_id), forwarded_headers)


def user_route_client(route: str, forwarded_headers: Mapping[str, str]) -> ChatRouteClient:
    """A client for *route*, a fixed path of the signed-in user's data, such as their memory."""
    port = env.read_int(env.CHAT_APP_PORT, fallback=3000)
    headers = {name: forwarded_headers[name] for name in IDENTITY_HEADERS if name in forwarded_headers}
    return ChatRouteClient(f"http://localhost:{port}{route}", headers)
