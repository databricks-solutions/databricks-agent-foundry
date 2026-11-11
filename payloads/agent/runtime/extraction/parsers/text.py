"""Plain-text formats: txt, md, json.  Decode-only, stdlib, no deps.

Uniform parser signature ``(data, max_rows) -> (text, truncated)``;
``max_rows`` is unused for prose formats but kept for a single dispatch
shape in :mod:`payloads.agent.runtime.extraction.core`.
"""

from __future__ import annotations

import json

from payloads.agent.runtime.extraction.models import ExtractionError


def parse_text(data: bytes, max_rows: int) -> tuple[str, bool]:
    # txt/md: bytes -> text.  Lossy decode (errors="replace") so a stray
    # non-UTF-8 byte degrades to U+FFFD instead of failing the upload.
    return data.decode("utf-8", errors="replace"), False


def parse_json(data: bytes, max_rows: int) -> tuple[str, bool]:
    raw = data.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ExtractionError(f"invalid JSON: {exc}") from exc
    # Re-emit pretty-printed so the model sees clean, stable structure
    # regardless of the source file's whitespace.
    return json.dumps(parsed, indent=2, ensure_ascii=False), False
