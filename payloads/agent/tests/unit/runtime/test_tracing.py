"""Tests for runtime tracing helpers (#444 duplicate-span / provider stamp)."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from mlflow.entities import SpanType
from mlflow.tracing.constant import SpanAttributeKey

from payloads.agent.runtime import tracing


class _FakeSpan:
    def __init__(self, span_type: str, attrs: dict[str, Any] | None = None) -> None:
        self.span_type = span_type
        self.attrs = dict(attrs or {})

    def get_attribute(self, key: str) -> Any:
        return self.attrs.get(key)

    def set_attribute(self, key: str, value: Any) -> None:
        self.attrs[key] = value


def test_llm_provider_from_databricks_client() -> None:
    class Client:
        pass

    client = Client()
    with patch("databricks_openai.AsyncDatabricksOpenAI", Client):
        assert tracing.llm_provider_for_client(client) == "databricks"


def test_llm_provider_falls_back_to_openai() -> None:
    assert tracing.llm_provider_for_client(object()) == "openai"


def test_stamp_skips_when_provider_unset() -> None:
    tracing._llm_provider = None
    span = _FakeSpan(SpanType.CHAT_MODEL)
    tracing.stamp_chat_model_provider(span)
    assert SpanAttributeKey.MODEL_PROVIDER not in span.attrs


def test_stamp_sets_provider_on_chat_model_only() -> None:
    tracing._llm_provider = "databricks"
    chat = _FakeSpan(SpanType.CHAT_MODEL)
    other = _FakeSpan(SpanType.CHAIN)
    tracing.stamp_chat_model_provider(chat)
    tracing.stamp_chat_model_provider(other)
    assert chat.attrs[SpanAttributeKey.MODEL_PROVIDER] == "databricks"
    assert SpanAttributeKey.MODEL_PROVIDER not in other.attrs


def test_stamp_does_not_overwrite_existing_provider() -> None:
    tracing._llm_provider = "databricks"
    span = _FakeSpan(SpanType.CHAT_MODEL, {SpanAttributeKey.MODEL_PROVIDER: "openai"})
    tracing.stamp_chat_model_provider(span)
    assert span.attrs[SpanAttributeKey.MODEL_PROVIDER] == "openai"


def test_configure_openai_autolog_disables_sdk_tracer_and_registers_processor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    autolog = MagicMock()
    disable = MagicMock()
    configure = MagicMock()
    monkeypatch.setattr(tracing, "_install_openai_autolog", autolog)
    monkeypatch.setattr(tracing, "_disable_agents_sdk_tracer", disable)
    monkeypatch.setattr("mlflow.tracing.configure", configure)
    monkeypatch.setattr("mlflow.tracing.config.get_config", lambda: MagicMock(span_processors=[]))
    monkeypatch.setattr(tracing, "llm_provider_for_client", lambda _client: "databricks")

    tracing.configure_openai_autolog(object())

    autolog.assert_called_once()
    disable.assert_called_once()
    configure.assert_called_once()
    processors = configure.call_args.kwargs["span_processors"]
    assert tracing.stamp_chat_model_provider in processors
    assert tracing._llm_provider == "databricks"
