"""Shared, dependency-light file-extraction core (D2).

Public surface: :func:`extract_file` plus the typed result/error and the
supported-format set.  Heavy parser deps (openpyxl, python-docx, pypdf)
are imported lazily inside the per-format parsers and declared under the
``extraction`` optional extra, so importing this package stays cheap and
the agent runtime never carries parser libraries it does not use.
"""

from payloads.agent.runtime.extraction.core import (
    DEFAULT_MAX_CHARS,
    DEFAULT_MAX_ROWS,
    detect_format,
    extract_file,
)
from payloads.agent.runtime.extraction.models import (
    SUPPORTED_FORMATS,
    ExtractedFile,
    ExtractionError,
)

__all__ = [
    "DEFAULT_MAX_CHARS",
    "DEFAULT_MAX_ROWS",
    "SUPPORTED_FORMATS",
    "ExtractedFile",
    "ExtractionError",
    "detect_format",
    "extract_file",
]
