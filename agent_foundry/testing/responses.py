"""Shared helpers and contract mixins for Responses API output in tests.

Used by local AgentServer tests and deployed-App acceptance tests.
"""

from __future__ import annotations

from typing import Any


def extract_message_items(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Return output items with type == 'message'."""
    return [item for item in body.get("output", []) if item.get("type") == "message"]


def extract_text(body: dict[str, Any]) -> str:
    """Concatenate all text content from message output items."""
    parts: list[str] = []
    for item in extract_message_items(body):
        for content in item.get("content", []):
            if "text" in content:
                parts.append(content["text"])
    return "".join(parts)


def extract_function_calls(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Return output items with type == 'function_call'."""
    return [item for item in body.get("output", []) if item.get("type") == "function_call"]


def extract_function_call_outputs(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Return output items with type == 'function_call_output'."""
    return [item for item in body.get("output", []) if item.get("type") == "function_call_output"]


class BasicInvocationContract:
    """Shared contract: basic Responses API invocation.

    Concrete classes must provide a ``simple_response`` fixture that returns
    a ``requests.Response`` (or httpx response) from a simple prompt.
    """

    def test_response_status_200(self, simple_response: Any) -> None:
        assert simple_response.status_code == 200, (
            f"Expected 200, got {simple_response.status_code}: {simple_response.text[:500]}"
        )

    def test_response_has_output_array(self, simple_response: Any) -> None:
        body = simple_response.json()
        assert "output" in body, f"Missing 'output' key: {list(body.keys())}"
        assert isinstance(body["output"], list)

    def test_output_contains_message_item(self, simple_response: Any) -> None:
        body = simple_response.json()
        messages = extract_message_items(body)
        assert len(messages) >= 1, f"No message items in output: {body.get('output', [])}"

    def test_message_has_text_content(self, simple_response: Any) -> None:
        text = extract_text(simple_response.json())
        assert len(text) > 0, "Agent returned empty text"


class ResponseFormatContract:
    """Shared contract: Responses API schema compliance.

    Concrete classes must provide a ``format_response_body`` fixture that
    returns the parsed JSON ``dict`` of a successful agent response.
    """

    def test_output_items_have_type_field(self, format_response_body: dict[str, Any]) -> None:
        for item in format_response_body["output"]:
            assert "type" in item, f"Output item missing 'type': {item}"

    def test_message_items_have_role(self, format_response_body: dict[str, Any]) -> None:
        for item in extract_message_items(format_response_body):
            assert item.get("role") == "assistant", f"Expected role='assistant', got {item.get('role')!r}"

    def test_content_items_have_type(self, format_response_body: dict[str, Any]) -> None:
        for item in extract_message_items(format_response_body):
            for content in item.get("content", []):
                assert "type" in content, f"Content item missing 'type': {content}"
