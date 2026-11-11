"""Tests for payloads/agent/runtime/content_filter.py."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from payloads.agent.runtime.content_filter import (
    CONTENT_FILTER_NOTICE,
    has_tool_calls,
    is_empty_content_filter,
)

REFUSED_TURNS_TS = Path(__file__).parents[3] / "ui/overrides/server/src/lib/refused-turns.ts"


def test_notice_tells_the_user_what_to_do() -> None:
    text = CONTENT_FILTER_NOTICE.lower()
    assert "declined" in text
    assert "rephrase" in text


@pytest.mark.parametrize("word", ["reasoning", "rules", "ignore", "filter", "safety"])
def test_notice_avoids_words_a_filter_may_take_for_a_jailbreak(word: str) -> None:
    assert word not in CONTENT_FILTER_NOTICE.lower()


def test_ui_server_recognises_the_same_notice() -> None:
    assert f'"{CONTENT_FILTER_NOTICE}"' in REFUSED_TURNS_TS.read_text(encoding="utf-8")


def test_recognises_openai_and_bedrock_finish_reasons() -> None:
    assert is_empty_content_filter("content_filter", text="", has_tool_calls=False)
    assert is_empty_content_filter("content_filtered", text=None, has_tool_calls=False)


def test_ignores_other_finish_reasons() -> None:
    assert not is_empty_content_filter("stop", text="", has_tool_calls=False)
    assert not is_empty_content_filter("length", text="", has_tool_calls=False)
    assert not is_empty_content_filter(None, text="", has_tool_calls=False)


def test_does_not_match_when_there_is_already_text() -> None:
    assert not is_empty_content_filter("content_filter", text="hello", has_tool_calls=False)


def test_does_not_match_when_the_model_called_a_tool() -> None:
    assert not is_empty_content_filter("content_filter", text="", has_tool_calls=True)


def test_has_tool_calls() -> None:
    assert not has_tool_calls(None)
    assert not has_tool_calls(SimpleNamespace(tool_calls=None))
    assert not has_tool_calls(SimpleNamespace(tool_calls=[]))
    assert has_tool_calls(SimpleNamespace(tool_calls=[{"id": "c1"}]))
