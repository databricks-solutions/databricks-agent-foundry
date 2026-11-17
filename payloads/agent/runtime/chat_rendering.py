"""Guidance on what the chat UI renders beyond plain Markdown.

Only the chat handlers in :mod:`payloads.agent.runtime.agent` add it:
``/api/mcp`` and A2A callers (``agent_runner.py``) have no UI to render
an ``svg`` or ``mermaid`` fence.

- Frontend renderers: ``payloads/agent/ui/overrides/client/src/components/elements/response.tsx``
- SVG size limit (must stay in sync): ``payloads/agent/ui/overrides/client/src/components/elements/svg-fence.ts``
"""

from __future__ import annotations

from typing import Final

MAX_SVG_CHARS: Final = 200_000

CHAT_RENDERING_GUIDANCE: Final = (
    "## Images and diagrams in the chat\n"
    "- The chat shows an `svg` fenced code block as an image, in an answer or inside a report. When the user "
    "asks for an illustration, icon, logo or other picture, draw it as SVG in an `svg` fence instead of saying "
    "you can't create images.\n"
    '- Start with `<svg xmlns="http://www.w3.org/2000/svg" ...>` and keep it self-contained: no DOCTYPE, and '
    f"scripts and links to outside images or fonts don't load. At most {MAX_SVG_CHARS:,} characters.\n"
    "- For flowcharts, sequence diagrams and similar diagrams, use a `mermaid` fence instead."
)
