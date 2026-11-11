"""Agent payload smoke probes: selection from the spec and reply-shape checks."""

from __future__ import annotations

import json
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.error import HTTPError

import pytest
import yaml

from agent_foundry.smoke import SmokeFailure, SmokeQuestion, SmokeTarget
from payloads.agent import smoke


def _instance(tmp_path: Path, *, mcp: bool, a2a: bool, user_skills: bool = False, compaction: bool = False) -> Path:
    payload_dir = tmp_path / "payloads" / "agent"
    payload_dir.mkdir(parents=True)
    spec: dict[str, Any] = {
        "integrations": {"mcp_server": {"enabled": mcp}, "a2a": {"enabled": a2a}},
        "runtime": {"context": {"compaction": {"enabled": compaction}}},
    }
    if user_skills:
        spec["runtime"]["tools"] = {"skills": {"user_skills": {"store": "lakebase"}}}
        spec["ui"] = {"chat_history": {"enabled": True}, "resources_sidebar": {"enabled": True}}
    manifest = {"payload": {"adapter": "agent", "spec": spec}}
    (payload_dir / "payload.yml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    return tmp_path


def _target(instance_dir: Path, questions: tuple[SmokeQuestion, ...] = ()) -> SmokeTarget:
    return SmokeTarget(
        ws=object(),
        app_url="https://app.example",
        workspace_host="https://ws.example",
        instance_dir=instance_dir,
        questions=questions,
        timeout_seconds=5,
    )


def _responses_body(text: str) -> dict[str, Any]:
    return {"output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}]}


@pytest.mark.parametrize(
    ("mcp", "a2a", "expected"),
    [
        (False, False, ["invocations"]),
        (True, False, ["invocations", "mcp list_tools", "mcp ask_agent"]),
        (False, True, ["invocations", "a2a agent card", "a2a SendMessage"]),
    ],
)
def test_probes_follow_the_spec(tmp_path: Path, mcp: bool, a2a: bool, expected: list[str]) -> None:
    names = [p.name.split(" '")[0] for p in smoke.agent_smoke_probes(_target(_instance(tmp_path, mcp=mcp, a2a=a2a)))]
    assert names == expected


def test_smoke_questions_add_invocation_probes(tmp_path: Path) -> None:
    question = SmokeQuestion(question="What is Unity Catalog?")
    target = _target(_instance(tmp_path, mcp=False, a2a=False), questions=(question,))
    names = [p.name for p in smoke.agent_smoke_probes(target)]
    assert names == [f"invocations {smoke.DEFAULT_PROMPT[:40]!r}", "invocations 'What is Unity Catalog?'"]


def test_invocations_requires_text(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def _post(_ws: object, url: str, body: dict[str, Any], **_kw: object) -> dict[str, Any]:
        calls.append((url, body))
        return _responses_body("hello")

    monkeypatch.setattr(smoke, "authenticated_post_json", _post)
    assert smoke.invocations_answer(_target(tmp_path), "hi") == "hello"
    request = {"input": [{"role": "user", "content": "hi"}], "stream": False}
    assert calls == [("https://app.example/invocations", request)]


def test_invocations_checks_expected_text(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(smoke, "authenticated_post_json", lambda *_a, **_kw: _responses_body("done TRIAGE-OK"))
    assert smoke.invocations_answer(_target(tmp_path), "hi", ["TRIAGE-OK"]) == "done TRIAGE-OK"
    with pytest.raises(SmokeFailure, match="MARKER"):
        smoke.invocations_answer(_target(tmp_path), "hi", ["MARKER"])

    monkeypatch.setattr(smoke, "authenticated_post_json", lambda *_a, **_k: {"output": []})
    with pytest.raises(SmokeFailure, match="no text"):
        smoke.invocations_answer(_target(tmp_path), "hi")


def test_user_skills_add_a_probe(tmp_path: Path) -> None:
    target = _target(_instance(tmp_path, mcp=False, a2a=False, user_skills=True))
    names = [p.name for p in smoke.agent_smoke_probes(target)]
    assert names[-1] == "invocations user skill"


def test_user_skill_probe_needs_the_code_word_from_the_body(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sent: list[dict[str, Any]] = []

    def _post(_ws: object, _url: str, body: dict[str, Any], **_kw: object) -> dict[str, Any]:
        sent.append(body)
        code_word = body["custom_inputs"]["user_skills"][0]["body"].rsplit(" ", 1)[-1]
        return _responses_body(code_word)

    monkeypatch.setattr(smoke, "authenticated_post_json", _post)
    answer = smoke.user_skill_answer(_target(tmp_path))

    skill = sent[0]["custom_inputs"]["user_skills"][0]
    assert skill["fqn"] == smoke.USER_SKILL_PROBE_FQN
    assert answer.startswith("probe-")
    assert answer not in skill["description"]

    monkeypatch.setattr(smoke, "authenticated_post_json", lambda *_a, **_kw: _responses_body("hello"))
    with pytest.raises(SmokeFailure, match="probe-"):
        smoke.user_skill_answer(_target(tmp_path))


def test_a2a_send_message_uses_v1_jsonrpc(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: dict[str, Any] = {}

    def _post(_ws: object, url: str, body: dict[str, Any], *, headers: dict[str, str], **_kw: object) -> dict[str, Any]:
        seen.update(url=url, body=body, headers=headers)
        return {"result": {"task": {"artifacts": [{"parts": [{"text": "hello"}]}]}}}

    monkeypatch.setattr(smoke, "authenticated_post_json", _post)
    assert smoke.a2a_send_message(_target(tmp_path), "hi") == "hello"
    assert seen["url"] == "https://app.example/api/a2a"
    assert seen["headers"] == {"A2A-Version": "1.0"}
    assert seen["body"]["method"] == "SendMessage"
    assert seen["body"]["params"]["message"]["parts"] == [{"text": "hi"}]


def test_a2a_error_reply_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(smoke, "authenticated_post_json", lambda *_a, **_k: {"error": {"code": -32001}})
    with pytest.raises(SmokeFailure, match="A2A error"):
        smoke.a2a_send_message(_target(tmp_path), "hi")


def test_agent_card_needs_a_name(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(smoke, "authenticated_get", lambda *_a, **_k: json.dumps({"name": "Supervisor"}).encode())
    assert smoke.a2a_agent_card(_target(tmp_path)) == "Supervisor"

    monkeypatch.setattr(smoke, "authenticated_get", lambda *_a, **_k: b"{}")
    with pytest.raises(SmokeFailure, match="no name"):
        smoke.a2a_agent_card(_target(tmp_path))


def test_mcp_list_requires_ask_agent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    tools = [SimpleNamespace(name="other")]
    monkeypatch.setattr("agent_foundry.testing.mcp_session.list_mcp_tools", lambda _ws, url: tools)
    with pytest.raises(SmokeFailure, match="'ask_agent' not in MCP tools"):
        smoke.mcp_lists_ask_agent(_target(tmp_path))


def test_mcp_ask_agent_reads_text_content(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: list[str] = []

    def _call(_ws: object, url: str, name: str, arguments: dict[str, Any]) -> object:
        seen.append(url)
        return SimpleNamespace(isError=False, content=[SimpleNamespace(text="hello")])

    monkeypatch.setattr("agent_foundry.testing.mcp_session.call_mcp_tool", _call)
    assert smoke.mcp_ask_agent(_target(tmp_path), "hi") == "hello"
    assert seen == ["https://app.example/api/mcp/"]


def test_collect_text_walks_nested_results() -> None:
    result = {"message": {"parts": [{"text": "a"}, {"data": {}}]}, "status": {"message": {"parts": [{"text": "b"}]}}}
    assert smoke.collect_text(result) == ["a", "b"]


class _FakeMemoryAgent:
    """Stands in for the deployed agent: remembers, recalls and forgets code words per actor."""

    def __init__(self, *, proxy_overwrites_actor: bool = True) -> None:
        self.proxy_overwrites_actor = proxy_overwrites_actor
        self.facts: dict[str, str] = {}
        self.calls: list[dict[str, Any]] = []

    def post(self, _ws: object, _url: str, body: dict[str, Any], *, headers: dict[str, str], **_kw: object) -> Any:
        self.calls.append({"body": body, "headers": headers})
        prompt = body["input"][0]["content"]
        key = "ci-sp" if self.proxy_overwrites_actor else headers.get("x-forwarded-user", "ci-sp")
        if "`_remember`" in prompt:
            self.facts[key] = prompt.split("code word is ")[1].split(".")[0]
            return _responses_body("done")
        if "`_forget`" in prompt:
            self.facts.pop(key, None)
            return _responses_body("forgotten")
        return _responses_body(self.facts.get(key, "none"))


def _context_instance(tmp_path: Path, context: dict[str, Any]) -> Path:
    instance = _instance(tmp_path, mcp=False, a2a=False)
    manifest_path = instance / "payloads" / "agent" / "payload.yml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["payload"]["spec"]["runtime"]["context"].update(context)
    manifest_path.write_text(yaml.safe_dump(manifest), encoding="utf-8")
    return instance


@pytest.mark.parametrize(
    ("context", "expected"),
    [
        ({"memory": {"enabled": True}}, ["invocations memory across chats"]),
        ({"plan": {"enabled": True}}, ["plan route checks chat access"]),
        ({}, []),
    ],
)
def test_context_features_add_their_probes(tmp_path: Path, context: dict[str, Any], expected: list[str]) -> None:
    names = [p.name for p in smoke.agent_smoke_probes(_target(_context_instance(tmp_path, context)))]
    assert names[1:] == expected


def test_memory_across_chats_passes_and_cleans_up(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    agent = _FakeMemoryAgent()
    monkeypatch.setattr(smoke, "authenticated_post_json", agent.post)
    assert smoke.memory_across_chats(_target(tmp_path)).startswith("probe-")
    first, *_ = agent.calls
    assert first["headers"] == smoke.FORGED_ACTOR
    assert len({call["body"]["context"]["conversation_id"] for call in agent.calls}) == 3
    assert agent.facts == {}


def test_memory_across_chats_fails_when_the_forged_actor_is_kept(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(smoke, "authenticated_post_json", _FakeMemoryAgent(proxy_overwrites_actor=False).post)
    with pytest.raises(SmokeFailure):
        smoke.memory_across_chats(_target(tmp_path))


def test_compaction_adds_the_history_search_probe(tmp_path: Path) -> None:
    instance = _instance(tmp_path, mcp=False, a2a=False, compaction=True)
    names = [p.name for p in smoke.agent_smoke_probes(_target(instance))]
    assert names[1:] == ["history search checks chat access"]


class _RefusingApp:
    """Stands in for the deployed app: answers every request with one HTTP error status."""

    def __init__(self, code: int) -> None:
        self.code = code
        self.urls: list[str] = []

    def send(self, _ws: Any, url: str, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        self.urls.append(url)
        raise HTTPError(url, self.code, "", Message(), None)


def test_history_search_probe_passes_on_403(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    app = _RefusingApp(403)
    monkeypatch.setattr(smoke, "authenticated_post_json", app.send)
    assert smoke.history_search_checks_access(_target(tmp_path)).endswith("403")
    (url,) = app.urls
    assert url.startswith("https://app.example/api/chat/")
    assert url.endswith("/history/search")


def test_history_search_probe_fails_when_it_answers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(smoke, "authenticated_post_json", lambda *_args, **_kwargs: {"hits": []})
    with pytest.raises(SmokeFailure, match="cannot read"):
        smoke.history_search_checks_access(_target(tmp_path))


def test_history_search_probe_fails_when_the_route_is_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(smoke, "authenticated_post_json", _RefusingApp(404).send)
    with pytest.raises(HTTPError):
        smoke.history_search_checks_access(_target(tmp_path))


def test_plan_probe_passes_on_403(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    app = _RefusingApp(403)
    monkeypatch.setattr(smoke, "authenticated_put_json", app.send)
    assert smoke.plan_route_checks_access(_target(tmp_path)).endswith("403")
    (url,) = app.urls
    assert url.startswith("https://app.example/api/chat/")
    assert url.endswith("/plan")


def test_plan_probe_fails_when_it_answers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(smoke, "authenticated_put_json", lambda *_args, **_kwargs: b"")
    with pytest.raises(SmokeFailure, match="the plan answered"):
        smoke.plan_route_checks_access(_target(tmp_path))


class _TranscribingApp:
    """Stands in for ``/api/transcribe``: fails with *statuses* first, then answers *text*."""

    def __init__(self, text: str, statuses: tuple[int, ...] = ()) -> None:
        self.text = text
        self.statuses = list(statuses)
        self.calls: list[dict[str, Any]] = []

    def post(self, _ws: object, url: str, data: bytes, **kwargs: Any) -> bytes:
        self.calls.append({"url": url, "data": data, **kwargs})
        if self.statuses:
            raise HTTPError(url, self.statuses.pop(0), "", Message(), None)
        return json.dumps({"text": self.text}).encode()


def test_speech_input_adds_the_transcribe_probe(tmp_path: Path) -> None:
    instance = _instance(tmp_path, mcp=False, a2a=False)
    manifest_path = instance / "payloads" / "agent" / "payload.yml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["payload"]["spec"]["ui"] = {"speech_input": {"enabled": True, "endpoint": "app-speech"}}
    manifest_path.write_text(yaml.safe_dump(manifest), encoding="utf-8")
    names = [p.name for p in smoke.agent_smoke_probes(_target(instance))]
    assert names[1:] == ["transcribe speech clip"]


def test_transcribe_probe_posts_the_packaged_clip(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    app = _TranscribingApp("The code word is Pineapple.")
    monkeypatch.setattr(smoke, "authenticated_post_bytes", app.post)
    assert smoke.transcribe_speech_clip(_target(tmp_path)) == "The code word is Pineapple."
    (call,) = app.calls
    assert call["url"] == "https://app.example/api/transcribe"
    assert call["content_type"] == "audio/mp4"
    assert call["headers"] == {"X-Speech-Language": "en"}
    assert call["data"][4:8] == b"ftyp"


def test_transcribe_probe_waits_for_the_endpoint_to_scale_up(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    app = _TranscribingApp("pineapple", statuses=(504, 503))
    sleeps: list[float] = []
    monkeypatch.setattr(smoke, "authenticated_post_bytes", app.post)
    assert smoke.transcribe_speech_clip(_target(tmp_path), sleep=sleeps.append) == "pineapple"
    assert sleeps == [smoke.SPEECH_WARMUP_SLEEP_SECONDS] * 2


def test_transcribe_probe_fails_fast_on_other_errors(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(smoke, "authenticated_post_bytes", _TranscribingApp("", statuses=(403,)).post)
    with pytest.raises(HTTPError):
        smoke.transcribe_speech_clip(_target(tmp_path), sleep=sleeps.append)
    assert sleeps == []


def test_transcribe_probe_needs_the_code_word(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(smoke, "authenticated_post_bytes", _TranscribingApp("hello there").post)
    with pytest.raises(SmokeFailure, match="pineapple"):
        smoke.transcribe_speech_clip(_target(tmp_path))
