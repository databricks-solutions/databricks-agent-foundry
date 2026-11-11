"""Tests for payloads/agent/runtime/context_limit.py."""

from __future__ import annotations

import pytest
from agents.exceptions import MaxTurnsExceeded

from payloads.agent.runtime.context_limit import (
    COMPACTING_CONTEXT_LIMIT_NOTICE,
    CONTEXT_LIMIT_MARKERS,
    CONTEXT_LIMIT_NOTICE,
    IMAGE_INPUT_NOTICE,
    MAX_TURNS_NOTICE,
    fail_soft_notice,
    is_context_limit_error,
)


@pytest.mark.parametrize("marker", CONTEXT_LIMIT_MARKERS)
def test_every_marker_is_recognised(marker: str) -> None:
    assert is_context_limit_error(RuntimeError(f"upstream said: {marker}"))


def test_recognises_the_anthropic_wording_verbatim() -> None:
    """The exact string the Databricks FMAPI passes through from Anthropic,
    which is the failure this module exists for.
    """
    exc = RuntimeError("Error code: 400 - {'error': {'message': 'prompt is too long: 214301 tokens > 200000 maximum'}}")
    assert is_context_limit_error(exc)


def test_matching_is_case_insensitive() -> None:
    assert is_context_limit_error(RuntimeError("PROMPT IS TOO LONG: 1 > 0"))


def test_unrelated_errors_are_not_matched() -> None:
    """Callers re-raise on a non-match, so a false positive here would
    silently swallow an unrelated failure.
    """
    assert not is_context_limit_error(RuntimeError("connection reset by peer"))
    assert not is_context_limit_error(ValueError("tool 'x' not found"))


def test_walks_the_cause_chain() -> None:
    """The marker is raised by the HTTP layer and re-wrapped before it
    reaches the agent loop, so only the chain reveals it.
    """
    root = RuntimeError("prompt is too long: 214301 tokens > 200000 maximum")
    wrapper = RuntimeError("model call failed")
    wrapper.__cause__ = root
    assert is_context_limit_error(wrapper)


def test_walks_the_implicit_context_chain() -> None:
    """A bare ``raise`` inside an ``except`` block sets ``__context__``
    rather than ``__cause__``.
    """
    root = RuntimeError("context_length_exceeded")
    wrapper = RuntimeError("model call failed")
    wrapper.__context__ = root
    assert is_context_limit_error(wrapper)


def test_cause_walk_is_depth_bounded() -> None:
    """A cyclic or pathological chain must not hang the error path."""
    first = RuntimeError("a")
    second = RuntimeError("b")
    first.__cause__ = second
    second.__cause__ = first
    assert not is_context_limit_error(first)


def test_notice_is_actionable() -> None:
    """The whole point of the notice is telling the user what to do and
    that the transcript survived; a bare "failed" would not.
    """
    assert "start a new conversation" in CONTEXT_LIMIT_NOTICE.lower()
    assert "lost" in CONTEXT_LIMIT_NOTICE.lower()


class TestFailSoftNotice:
    def test_context_limit_gets_its_notice(self) -> None:
        assert fail_soft_notice(RuntimeError("prompt is too long: 1 > 0")) == CONTEXT_LIMIT_NOTICE

    def test_compaction_turns_the_notice_into_a_retry(self) -> None:
        exc = RuntimeError("prompt is too long: 1 > 0")
        notice = fail_soft_notice(exc, compaction=True)
        assert notice == COMPACTING_CONTEXT_LIMIT_NOTICE
        assert "send your message again" in notice
        assert "new conversation" not in notice

    def test_compaction_leaves_the_image_notice(self) -> None:
        exc = RuntimeError("Image input is not supported for this endpoint")
        assert fail_soft_notice(exc, compaction=True) == IMAGE_INPUT_NOTICE

    def test_image_on_text_only_model_gets_its_notice(self) -> None:
        """The wording Databricks serving and the Unity Gateway return, wrapped as the SDK raises it."""
        root = RuntimeError("Error code: 400 - {'message': 'Image input is not supported for this endpoint'}")
        wrapper = RuntimeError("model call failed")
        wrapper.__cause__ = root

        assert fail_soft_notice(wrapper) == IMAGE_INPUT_NOTICE

    def test_unrelated_error_returns_none(self) -> None:
        assert fail_soft_notice(RuntimeError("connection reset by peer")) is None

    def test_image_notice_says_to_start_without_the_image(self) -> None:
        assert "start a new conversation without the image" in IMAGE_INPUT_NOTICE.lower()

    @pytest.mark.parametrize("compaction", [False, True])
    def test_step_limit_says_to_continue(self, compaction: bool) -> None:
        notice = fail_soft_notice(MaxTurnsExceeded("Max turns (10) exceeded"), compaction=compaction)
        assert notice == MAX_TURNS_NOTICE
        assert '"continue"' in notice
