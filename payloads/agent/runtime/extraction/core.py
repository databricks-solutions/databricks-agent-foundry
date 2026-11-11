"""Format dispatch + context-budget enforcement for file extraction.

The single entry point is :func:`extract_file`.  It is pure and
stateless (no disk, no network, no agent/MCP/Databricks coupling) so the
same code backs the HTTP extractor service and an in-process tool alike
(D2).  The hard upload *size* cap and per-parse timeout/resource limits
are the caller/service wrapper's responsibility (§7); this module owns
only the context-budget caps (O2).
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Callable

from payloads.agent.runtime.extraction.models import (
    SUPPORTED_FORMATS,
    ExtractedFile,
    ExtractionError,
)
from payloads.agent.runtime.extraction.parsers import documents, tabular, text

# O2 defaults: format-aware budget.  Prose is char-capped; tabular is
# row-capped during parse.  ~50k chars / 2k rows ~= 12k tokens per file.
# Tunable by the caller (the service reads env-configured overrides).
DEFAULT_MAX_CHARS = 50_000
DEFAULT_MAX_ROWS = 2_000

# canonical format -> parser.  Parser signature is uniform:
# ``(data: bytes, max_rows: int) -> (rendered_text, truncated)``.
_PARSERS: dict[str, Callable[[bytes, int], tuple[str, bool]]] = {
    "xlsx": tabular.parse_xlsx,
    "csv": tabular.parse_csv,
    "docx": documents.parse_docx,
    "pdf": documents.parse_pdf,
    "txt": text.parse_text,
    "md": text.parse_text,
    "json": text.parse_json,
}


def detect_format(filename: str) -> str:
    """Map a filename to a supported canonical format by extension.

    Raises :class:`ExtractionError` for unsupported / extensionless
    names so the caller can reject the upload with a clear message.
    """
    suffix = PurePosixPath(filename).suffix.lower().lstrip(".")
    if suffix not in SUPPORTED_FORMATS:
        raise ExtractionError(
            f"unsupported file type {suffix or '(none)'!r}; supported: {', '.join(sorted(SUPPORTED_FORMATS))}",
        )
    return suffix


def extract_file(
    data: bytes,
    filename: str,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    max_rows: int = DEFAULT_MAX_ROWS,
) -> ExtractedFile:
    """Extract ``data`` (raw bytes of ``filename``) to model-ready text."""
    fmt = detect_format(filename)
    parser = _PARSERS[fmt]
    body, parse_truncated = parser(data, max_rows)
    capped, char_truncated = _cap_chars(body, max_chars)
    return ExtractedFile(
        filename=filename,
        format=fmt,
        text=capped,
        truncated=parse_truncated or char_truncated,
    )


def _cap_chars(body: str, max_chars: int) -> tuple[str, bool]:
    if max_chars <= 0 or len(body) <= max_chars:
        return body, False
    notice = f"\n\n[truncated: showing first {max_chars} of {len(body)} characters]"
    return body[:max_chars] + notice, True
