"""Out-of-process parse worker for the file extractor (§7 hardening).

Each upload is parsed in a disposable ``spawn``ed subprocess so a
malformed / malicious file cannot take down the extractor service:

* **Timeout** -- the parent kills a worker that overruns the budget.
* **Crash / OOM isolation** -- a segfault or memory-limit abort dies with
  the child; the parent maps it to a clean HTTP error.
* **XML hardening** -- ``defusedxml.defuse_stdlib()`` neutralises entity
  attacks on ``openpyxl``'s stdlib XML path (``python-docx`` already
  parses with ``resolve_entities=False``).
* **Address-space ceiling** -- optional ``FILE_UPLOAD_MEM_LIMIT_MB`` caps
  a single parse (POSIX only; unset = rely on the container limit).

Deliberately free of heavy / web imports (no FastAPI) so the spawned
interpreter starts fast and carries only the extraction core.
"""

from __future__ import annotations

import multiprocessing
from typing import Any, cast

from agent_foundry import env as _env
from payloads.agent.runtime.extraction import ExtractionError, extract_file

# spawn (not fork): a clean interpreter per parse with no locks / fds
# inherited from the async server -- safer for untrusted-input parsing.
_MP_CONTEXT = multiprocessing.get_context("spawn")


class ExtractionTimeout(Exception):
    """A single parse exceeded its wall-clock budget and was killed."""


class ExtractionCrash(Exception):
    """A parse worker exited without a result (segfault / OOM / rlimit)."""


def _apply_memory_ceiling() -> None:
    """Cap the worker's address space when ``FILE_UPLOAD_MEM_LIMIT_MB`` is set.

    Best-effort and POSIX-only: a memory bomb then OOMs only this
    disposable child, never the service.  No-ops when unset or unsupported.
    """
    megabytes = _env.read_int(_env.FILE_UPLOAD_MEM_LIMIT_MB)
    if not megabytes:
        return
    try:
        import resource
    except ImportError:
        return
    limit = megabytes * 1024 * 1024
    try:
        _soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        ceiling = limit if hard == resource.RLIM_INFINITY else min(limit, hard)
        resource.setrlimit(resource.RLIMIT_AS, (ceiling, hard))
    except (ValueError, OSError):
        return


def _harden_xml() -> None:
    """Neutralise stdlib XML entity attacks (XXE / billion-laughs).

    No-ops when ``defusedxml`` is not installed (the ``extraction`` extra
    pins it); ``python-docx`` is already safe via ``resolve_entities=False``.
    """
    try:
        import defusedxml
    except ImportError:
        return
    defusedxml.defuse_stdlib()


def _worker(data: bytes, filename: str, queue: Any) -> None:
    """Parse in the child and report a tagged ``(status, payload)`` result.

    Never re-raises across the process boundary: every outcome is encoded
    so the parent maps it to an HTTP code without unpickling arbitrary
    exceptions.
    """
    _apply_memory_ceiling()
    _harden_xml()
    try:
        result = extract_file(data, filename)
        queue.put(("ok", result.model_dump()))
    except ExtractionError as exc:
        queue.put(("rejected", str(exc)))
    except Exception:
        # Contain *any* parser failure; surface a generic, user-safe note.
        queue.put(("crashed", "could not extract this file"))


def run_isolated(data: bytes, filename: str, timeout: int) -> dict[str, Any]:
    """Parse *data* in a spawned worker, killing it on timeout.

    Blocking; callers in an async context should use ``asyncio.to_thread``.
    Returns the ``ExtractedFile`` payload dict, or raises
    :class:`ExtractionError` / :class:`ExtractionTimeout` /
    :class:`ExtractionCrash`.
    """
    queue: Any = _MP_CONTEXT.Queue()
    process = _MP_CONTEXT.Process(target=_worker, args=(data, filename, queue))
    process.start()
    process.join(timeout)
    if process.is_alive():
        process.terminate()
        process.join()
        raise ExtractionTimeout
    if queue.empty():
        raise ExtractionCrash
    status, payload = queue.get()
    if status == "ok":
        return cast(dict[str, Any], payload)
    if status == "rejected":
        raise ExtractionError(payload)
    raise ExtractionCrash
