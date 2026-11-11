"""Container-safety guards for untrusted uploads (file-upload §7).

The Office Open XML formats (``.xlsx`` / ``.docx``) are ZIP archives of
XML.  Before handing bytes to ``openpyxl`` / ``python-docx`` we cheaply
inspect the central directory to reject the classic decompression-bomb
shapes (huge entry counts, implausible uncompressed totals, extreme
compression ratios).

Living in the shared core means every caller -- the isolated extractor
service *and* (later) the in-chat path -- inherits the guard (DRY).  It
is a first, cheap line of defence on attacker-controlled metadata; it
complements (does not replace) the process isolation, per-parse timeout,
and hard upload size cap enforced by the extractor service.  XXE is
handled separately at parse time (``python-docx`` parses with
``resolve_entities=False``; the worker calls ``defusedxml.defuse_stdlib``
to harden ``openpyxl``'s stdlib XML path).
"""

from __future__ import annotations

import io
import zipfile

from payloads.agent.runtime.extraction.models import ExtractionError

# Conservative ceilings sized for a ~10 MB upload cap.  Centralised here
# rather than threaded through parser signatures to keep them clean.
MAX_ZIP_ENTRIES = 1000
MAX_ZIP_UNCOMPRESSED_BYTES = 200 * 1024 * 1024  # 200 MB
MAX_ZIP_COMPRESSION_RATIO = 100


def guard_zip(data: bytes) -> None:
    """Reject decompression-bomb-shaped Office Open XML archives.

    Raises :class:`ExtractionError` (user-safe message) when *data* is not
    a valid zip or exceeds the entry-count / uncompressed-size / ratio
    ceilings.  Returns ``None`` when the archive looks safe to parse.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
    except zipfile.BadZipFile as exc:
        raise ExtractionError("file is not a valid Office Open XML (zip) document") from exc

    if len(infos) > MAX_ZIP_ENTRIES:
        raise ExtractionError("file rejected: too many entries in the archive")

    total_uncompressed = sum(info.file_size for info in infos)
    if total_uncompressed > MAX_ZIP_UNCOMPRESSED_BYTES:
        raise ExtractionError("file rejected: decompressed size exceeds the safety limit")

    total_compressed = sum(info.compress_size for info in infos) or 1
    if total_uncompressed / total_compressed > MAX_ZIP_COMPRESSION_RATIO:
        raise ExtractionError("file rejected: compression ratio exceeds the safety limit")
