"""Tests for payloads/agent/runtime/chat_rendering.py."""

from __future__ import annotations

from pathlib import Path

from payloads.agent.runtime.chat_rendering import CHAT_RENDERING_GUIDANCE, MAX_SVG_CHARS

SVG_FENCE_TS = Path(__file__).parents[3] / "ui/overrides/client/src/components/elements/svg-fence.ts"


def test_svg_size_limit_matches_the_ui() -> None:
    assert f"export const MAX_SVG_CHARS = {MAX_SVG_CHARS:_};" in SVG_FENCE_TS.read_text(encoding="utf-8")


def test_guidance_names_the_fences_the_ui_renders() -> None:
    assert "`svg` fence" in CHAT_RENDERING_GUIDANCE
    assert "`mermaid` fence" in CHAT_RENDERING_GUIDANCE
    assert f"{MAX_SVG_CHARS:,} characters" in CHAT_RENDERING_GUIDANCE
