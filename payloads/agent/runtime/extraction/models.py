"""Typed results + errors for the shared file-extraction core.

Parsing lives in one dependency-light module reused by the chat upload path and
(later) the PPC config tab.
"""

from __future__ import annotations

from pydantic import BaseModel

# v1 formats (O3).  Extension-keyed; see ``core.detect_format``.  No OCR
# (scanned PDFs unsupported), no legacy ``.xls``.
SUPPORTED_FORMATS: frozenset[str] = frozenset(
    {"xlsx", "csv", "docx", "pdf", "txt", "md", "json"},
)


class ExtractionError(Exception):
    """Raised for an unsupported format, a missing optional parser, or a
    parse failure on malformed input.

    Carries a user-safe message: callers surface it to the chat UI as a
    rejection notice, so it must never leak internals or raw input.
    """


class ExtractedFile(BaseModel, frozen=True):
    """Result of extracting one uploaded file to model-ready text.

    ``text`` is already budget-capped by ``extract_file``; ``truncated``
    flags that content was dropped to fit the budget so callers can show
    a notice.  The contract is deliberately text-first for the chat path;
    parser internals stay structured enough that the PPC config tab can
    later return rows/sections without breaking this shape.
    """

    filename: str
    format: str
    text: str
    truncated: bool = False
