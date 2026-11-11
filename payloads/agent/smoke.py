"""Smoke probes for the agent payload: ``/invocations``, MCP and A2A, as an external caller.

Which protocols are probed follows the payload spec
(``spec.integrations.mcp_server.enabled`` / ``spec.integrations.a2a.enabled``).
With ``spec.runtime.tools.skills.user_skills`` on, one more ``/invocations``
call sends a user skill whose body holds a fresh code word, so only an
answer that loaded it through ``_load_skill`` passes.  With
``spec.runtime.context.memory`` on, a code word is remembered in one
conversation and recalled in another (with a forged ``x-forwarded-user`` the
proxy must overwrite).  With ``spec.runtime.context.plan`` on, the plan
route, and with compaction on, the history search route, must refuse a chat
the caller cannot read.  With ``spec.ui.speech_input`` on, a synthetic
speech clip (macOS ``say``, no personal voice) must transcribe to its code word.
Assertions are shape-only -- a reply with text -- unless a ``smoke.yml``
question names ``expect_contains`` substrings.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from functools import partial
from importlib import resources
from typing import Any, Final
from urllib.error import HTTPError

from agent_foundry.smoke import (
    SmokeFailure,
    SmokeProbe,
    SmokeQuestion,
    SmokeTarget,
    require_contains,
    require_text,
)
from agent_foundry.testing.readiness import (
    authenticated_get,
    authenticated_post_bytes,
    authenticated_post_json,
    authenticated_put_json,
)
from agent_foundry.testing.responses import extract_text

DEFAULT_PROMPT: Final = "Reply with the single word hello."
MCP_TOOL: Final = "ask_agent"
AGENT_CARD_PATH: Final = "/api/.well-known/agent-card.json"
A2A_RPC_PATH: Final = "/api/a2a"
A2A_VERSION_HEADERS: Final = {"A2A-Version": "1.0"}
USER_SKILL_PROBE_FQN: Final = "lakebase:smoke-probe"
MEMORY_PROBE_TOPIC: Final = "smoke-probe"
FORGED_ACTOR: Final = {"x-forwarded-user": "forged-smoke-actor"}
TRANSCRIBE_PATH: Final = "/api/transcribe"
SPEECH_PROBE_CLIP: Final = "speech_probe.m4a"
SPEECH_PROBE_WORD: Final = "pineapple"
# A scale-to-zero GPU endpoint answers 502/503/504 until it has scaled up.
SPEECH_WARMUP_STATUSES: Final = frozenset({502, 503, 504})
SPEECH_WARMUP_ATTEMPTS: Final = 12
SPEECH_WARMUP_SLEEP_SECONDS: Final = 30
_NAME_PREVIEW: Final = 40


def agent_smoke_probes(target: SmokeTarget) -> list[SmokeProbe]:
    from payloads.agent.config import load_agent_spec

    spec = load_agent_spec(target.instance_dir)
    integrations = spec.integrations
    questions = (SmokeQuestion(question=DEFAULT_PROMPT), *target.questions)
    probes = [_invocations_probe(target, question) for question in questions]
    if spec.runtime.tools.skills.lakebase_user_skills() is not None:
        probes.append(SmokeProbe("invocations user skill", partial(user_skill_answer, target)))
    if spec.memory_enabled():
        probes.append(SmokeProbe("invocations memory across chats", partial(memory_across_chats, target)))
    if spec.plan_enabled():
        probes.append(SmokeProbe("plan route checks chat access", partial(plan_route_checks_access, target)))
    if spec.compaction_enabled():
        probes.append(SmokeProbe("history search checks chat access", partial(history_search_checks_access, target)))
    if spec.ui.speech_input.enabled:
        probes.append(SmokeProbe("transcribe speech clip", partial(transcribe_speech_clip, target)))
    if integrations.mcp_server.enabled:
        probes += [
            SmokeProbe("mcp list_tools", partial(mcp_lists_ask_agent, target)),
            SmokeProbe("mcp ask_agent", partial(mcp_ask_agent, target, DEFAULT_PROMPT)),
        ]
    if integrations.a2a.enabled:
        probes += [
            SmokeProbe("a2a agent card", partial(a2a_agent_card, target)),
            SmokeProbe("a2a SendMessage", partial(a2a_send_message, target, DEFAULT_PROMPT)),
        ]
    return probes


def invocations_answer(
    target: SmokeTarget,
    question: str,
    expect_contains: Sequence[str] = (),
    *,
    custom_inputs: Mapping[str, Any] | None = None,
    conversation_id: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> str:
    request: dict[str, Any] = {"input": [{"role": "user", "content": question}], "stream": False}
    if custom_inputs is not None:
        request["custom_inputs"] = dict(custom_inputs)
    if conversation_id is not None:
        request["context"] = {"conversation_id": conversation_id}
    body = authenticated_post_json(
        target.ws,
        f"{target.app_url}/invocations",
        request,
        headers=dict(headers or {}),
        timeout_seconds=target.timeout_seconds,
    )
    text = extract_text(body)
    preview = require_text(text, "/invocations")
    require_contains(text, expect_contains, "/invocations")
    return preview


def user_skill_answer(target: SmokeTarget) -> str:
    code_word = f"probe-{uuid.uuid4().hex[:8]}"
    skill = {
        "fqn": USER_SKILL_PROBE_FQN,
        "display_name": "smoke-probe",
        "description": "Smoke test: load it whenever the user asks you to.",
        "body": f"Reply with only this code word: {code_word}",
    }
    question = f"Load the skill `{USER_SKILL_PROBE_FQN}` and do what it says."
    return invocations_answer(target, question, (code_word,), custom_inputs={"user_skills": [skill]})


def _new_conversation() -> str:
    return f"smoke-{uuid.uuid4().hex[:12]}"


def _memory_fact(code_word: str) -> str:
    return f"The smoke code word is {code_word}"


def _remember_prompt(code_word: str) -> str:
    return (
        f'Use the `_remember` tool with topic "{MEMORY_PROBE_TOPIC}" '
        f"to save this fact: {_memory_fact(code_word)}. Then reply with done."
    )


_RECALL_PROMPT: Final = (
    "Use the `_recall` tool to find the smoke code word. "
    "Reply with only the code word, or with none if memory has none."
)


def _forget_prompt(code_word: str) -> str:
    return f'Use the `_forget` tool with topic "{MEMORY_PROBE_TOPIC}" to remove this fact: {_memory_fact(code_word)}.'


def memory_across_chats(target: SmokeTarget) -> str:
    """User memory saved in one conversation is recalled in another.

    The save forges ``x-forwarded-user``; the Apps proxy must overwrite it,
    or the fact lands under the forged actor and the recall misses it.
    """
    code_word = f"probe-{uuid.uuid4().hex[:8]}"
    invocations_answer(target, _remember_prompt(code_word), conversation_id=_new_conversation(), headers=FORGED_ACTOR)
    try:
        return invocations_answer(target, _RECALL_PROMPT, (code_word,), conversation_id=_new_conversation())
    finally:
        invocations_answer(target, _forget_prompt(code_word), conversation_id=_new_conversation())


def _foreign_chat_url(target: SmokeTarget, route: str) -> str:
    return f"{target.app_url}{route.format(chat_id=uuid.uuid4())}"


def _expect_403(send: Callable[[], object], route_name: str) -> str:
    """Pass when *send* answers 403: the route is mounted and refuses a chat the caller cannot read."""
    try:
        send()
    except HTTPError as exc:
        if exc.code == 403:
            return "a chat without access answers 403"
        raise
    raise SmokeFailure(f"{route_name} answered for a chat the caller cannot read")


def plan_route_checks_access(target: SmokeTarget) -> str:
    """Writing the plan of a chat the caller does not own answers 403.

    A write, because reading the plan of a chat not saved yet answers an empty plan.
    """
    from payloads.agent.runtime.plan_tools import PLAN_ROUTE

    url = _foreign_chat_url(target, PLAN_ROUTE)
    empty_plan = {"content": "", "history": ""}
    return _expect_403(
        lambda: authenticated_put_json(target.ws, url, empty_plan, timeout_seconds=target.timeout_seconds), "the plan"
    )


def history_search_checks_access(target: SmokeTarget) -> str:
    """The history search of a chat the caller cannot read answers 403."""
    from payloads.agent.runtime.history_search import HISTORY_SEARCH_ROUTE

    url = _foreign_chat_url(target, HISTORY_SEARCH_ROUTE)
    return _expect_403(
        lambda: authenticated_post_json(target.ws, url, {"query": "smoke"}, timeout_seconds=target.timeout_seconds),
        "history search",
    )


def _post_speech_clip(target: SmokeTarget, clip: bytes) -> bytes:
    return authenticated_post_bytes(
        target.ws,
        f"{target.app_url}{TRANSCRIBE_PATH}",
        clip,
        content_type="audio/mp4",
        headers={"X-Speech-Language": "en"},
        timeout_seconds=target.timeout_seconds,
    )


def _post_speech_clip_warm(target: SmokeTarget, clip: bytes, sleep: Callable[[float], None]) -> bytes:
    """Post the clip, retrying while the speech endpoint scales up from zero."""
    for _ in range(SPEECH_WARMUP_ATTEMPTS - 1):
        try:
            return _post_speech_clip(target, clip)
        except HTTPError as exc:
            if exc.code not in SPEECH_WARMUP_STATUSES:
                raise
        sleep(SPEECH_WARMUP_SLEEP_SECONDS)
    return _post_speech_clip(target, clip)


def transcribe_speech_clip(target: SmokeTarget, sleep: Callable[[float], None] = time.sleep) -> str:
    """The packaged synthetic clip transcribes through ``/api/transcribe`` to its code word."""
    clip = resources.files("payloads.agent").joinpath("smoke_data", SPEECH_PROBE_CLIP).read_bytes()
    reply = json.loads(_post_speech_clip_warm(target, clip, sleep).decode("utf-8"))
    text = str(reply.get("text", "")) if isinstance(reply, dict) else ""
    require_contains(text.lower(), (SPEECH_PROBE_WORD,), TRANSCRIBE_PATH)
    return require_text(text, TRANSCRIBE_PATH)


def mcp_lists_ask_agent(target: SmokeTarget) -> str:
    from agent_foundry.testing.mcp_session import list_mcp_tools

    names = sorted(tool.name for tool in list_mcp_tools(target.ws, _mcp_url(target)))
    if MCP_TOOL not in names:
        raise SmokeFailure(f"{MCP_TOOL!r} not in MCP tools {names}")
    return ", ".join(names)


def mcp_ask_agent(target: SmokeTarget, question: str) -> str:
    from agent_foundry.testing.mcp_session import call_mcp_tool

    result = call_mcp_tool(target.ws, _mcp_url(target), MCP_TOOL, {"question": question})
    if getattr(result, "isError", False):
        raise SmokeFailure(f"{MCP_TOOL} returned an error: {result}")
    return require_text(_mcp_result_text(result), MCP_TOOL)


def a2a_agent_card(target: SmokeTarget) -> str:
    raw = authenticated_get(target.ws, f"{target.app_url}{AGENT_CARD_PATH}", timeout_seconds=target.timeout_seconds)
    card = json.loads(raw.decode("utf-8"))
    name = card.get("name") if isinstance(card, dict) else None
    if not name:
        raise SmokeFailure(f"agent card has no name: {raw[:200]!r}")
    return str(name)


def a2a_send_message(target: SmokeTarget, question: str) -> str:
    request = {
        "jsonrpc": "2.0",
        "id": "smoke",
        "method": "SendMessage",
        "params": {
            "message": {
                "messageId": uuid.uuid4().hex,
                "role": "ROLE_USER",
                "parts": [{"text": question}],
            }
        },
    }
    body = authenticated_post_json(
        target.ws,
        f"{target.app_url}{A2A_RPC_PATH}",
        request,
        headers=A2A_VERSION_HEADERS,
        timeout_seconds=target.timeout_seconds,
    )
    if body.get("error"):
        raise SmokeFailure(f"A2A error: {body['error']}")
    return require_text(" ".join(collect_text(body.get("result"))), "A2A SendMessage")


def collect_text(node: Any) -> list[str]:
    """Every ``text`` string in an A2A result (message parts, task artifacts, status)."""
    if isinstance(node, dict):
        own = [node["text"]] if isinstance(node.get("text"), str) else []
        return own + [text for key, value in node.items() if key != "text" for text in collect_text(value)]
    if isinstance(node, list):
        return [text for item in node for text in collect_text(item)]
    return []


def _invocations_probe(target: SmokeTarget, question: SmokeQuestion) -> SmokeProbe:
    return SmokeProbe(
        f"invocations {question.question[:_NAME_PREVIEW]!r}",
        partial(invocations_answer, target, question.question, question.expect_contains),
    )


def _mcp_url(target: SmokeTarget) -> str:
    return f"{target.app_url}/api/mcp/"


def _mcp_result_text(result: Any) -> str:
    content = getattr(result, "content", None) or []
    return " ".join(str(getattr(item, "text", "")) for item in content)


__all__ = [
    "agent_smoke_probes",
    "a2a_agent_card",
    "a2a_send_message",
    "collect_text",
    "invocations_answer",
    "mcp_ask_agent",
    "mcp_lists_ask_agent",
    "memory_across_chats",
    "plan_route_checks_access",
    "transcribe_speech_clip",
    "user_skill_answer",
]
