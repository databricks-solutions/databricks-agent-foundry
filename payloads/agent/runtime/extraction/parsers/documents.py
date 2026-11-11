"""Document formats: docx (python-docx) and pdf (pypdf).  Both optional.

Text-only extraction (O3): docx paragraph text, pdf page text.  No OCR,
so scanned/image PDFs yield little or no text.  Heavy deps are imported
lazily and surfaced as a clear :class:`ExtractionError` when absent.
"""

from __future__ import annotations

import io

from payloads.agent.runtime.extraction.models import ExtractionError
from payloads.agent.runtime.extraction.safety import guard_zip


def parse_docx(data: bytes, max_rows: int) -> tuple[str, bool]:
    # Reject decompression-bomb-shaped archives before python-docx touches
    # the zip+XML container (§7).  python-docx parses with
    # resolve_entities=False, so the XXE vector is already closed here.
    guard_zip(data)
    try:
        import docx
    except ImportError as exc:
        raise ExtractionError(
            "docx extraction requires the 'python-docx' package (install agent-foundry[extraction])",
        ) from exc
    document = docx.Document(io.BytesIO(data))
    blocks = [paragraph.text for paragraph in document.paragraphs if paragraph.text.strip()]
    return "\n\n".join(blocks), False


def parse_pdf(data: bytes, max_rows: int) -> tuple[str, bool]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ExtractionError(
            "pdf extraction requires the 'pypdf' package (install agent-foundry[extraction])",
        ) from exc
    reader = PdfReader(io.BytesIO(data))
    pages = [(page.extract_text() or "").strip() for page in reader.pages]
    return "\n\n".join(page for page in pages if page), False
