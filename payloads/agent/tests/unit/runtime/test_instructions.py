"""Tests for :mod:`payloads.agent.runtime.instructions`."""

from __future__ import annotations

import re

from payloads.agent.runtime.instructions import build_context_preamble


class TestBuildContextPreamble:
    def test_format_structure(self) -> None:
        preamble = build_context_preamble()
        assert preamble.startswith("[Context:")
        assert preamble.endswith("]")
        assert "| User:" in preamble

    def test_contains_utc_marker(self) -> None:
        preamble = build_context_preamble()
        assert "UTC" in preamble

    def test_contains_date_and_minute(self) -> None:
        preamble = build_context_preamble()
        assert re.search(r"\w{3} \w{3} \d{2} \d{4} \d{2}:\d{2} \(UTC\)", preamble)

    def test_user_defaults_to_unknown(self) -> None:
        preamble = build_context_preamble()
        assert "User: unknown" in preamble

    def test_user_passed_explicitly(self) -> None:
        preamble = build_context_preamble("alice@example.com")
        assert "User: alice@example.com" in preamble
