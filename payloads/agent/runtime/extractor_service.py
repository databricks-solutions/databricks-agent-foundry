"""Isolated file-extraction HTTP service (D6 + §7 hardening).

A minimal FastAPI app wrapping the dependency-light
``payloads.agent.runtime.extraction`` core in its own process (default
``:8001``), supervised by ``start_app.py`` and launched only when
``ENABLE_FILE_UPLOAD`` is truthy.

Running extraction out-of-process is a deliberate isolation boundary
(§7): a crash / OOM / exploit while parsing a malformed, oversized, or
malicious upload cannot take down the agent backend on ``:8000``.  This
module owns the request-shaped guards; the per-parse guards (timeout-kill,
XXE defuse, memory ceiling, crash isolation) live in
``extract_worker.run_isolated``, and the zip-bomb guard lives in the
shared core (``payloads.agent.runtime.extraction.safety``).

Endpoints
---------
* ``GET  /health``  -> ``{"status": "ok"}``
* ``POST /extract`` -> multipart ``file`` -> ``{filename, format, text, truncated}``
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, FastAPI, File, HTTPException, UploadFile
from starlette.responses import JSONResponse

from agent_foundry import env as _env
from payloads.agent.runtime.extract_worker import (
    ExtractionCrash,
    ExtractionTimeout,
    run_isolated,
)
from payloads.agent.runtime.extraction import ExtractionError

# Per-parse wall-clock budget; a runaway parse is killed by the worker.
_PARSE_TIMEOUT_SECONDS = 30
# Slack above the file cap for the multipart envelope (boundaries + part
# headers).  The wire-level ceiling is ``cap + slack``; the file content
# itself is re-checked against the cap after parsing.
_MULTIPART_SLACK_BYTES = 1024 * 1024

router = APIRouter()


def _max_upload_bytes() -> int:
    megabytes = _env.read_int(_env.FILE_UPLOAD_MAX_MB, fallback=10) or 10
    return megabytes * 1024 * 1024


def _too_large_detail(limit: int) -> str:
    return f"file exceeds the {limit // (1024 * 1024)} MB upload cap"


class _ReplayReceive:
    """ASGI ``receive`` that yields a pre-read body once, then delegates.

    Lets the size middleware drain + validate the body up front and still
    hand the app a normal request stream (e.g. for the multipart parser).
    """

    def __init__(self, body: bytes, original: Any) -> None:
        self._body = body
        self._original = original
        self._sent = False

    async def __call__(self) -> Any:
        if not self._sent:
            self._sent = True
            return {"type": "http.request", "body": self._body, "more_body": False}
        return await self._original()


class MaxBodySizeMiddleware:
    """Pure-ASGI guard that caps the request body at ``cap + slack`` bytes.

    A header check trusts a client-supplied Content-Length; draining and
    counting actual bytes also covers a lying length or a chunked body with
    no length at all (the residual called out in §7).  The buffer is bounded
    by the ceiling, so an oversized upload is rejected without unbounded
    memory growth; a within-limit body is replayed to the app unchanged.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = _max_upload_bytes()
        ceiling = limit + _MULTIPART_SLACK_BYTES
        declared = _declared_content_length(scope)
        if declared is not None and declared > ceiling:
            # Fast path: reject before reading any of the body.
            await self._reject(limit, scope, receive, send)
            return
        body = bytearray()
        more = True
        while more:
            message = await receive()
            if message["type"] != "http.request":
                break
            body.extend(message.get("body", b""))
            if len(body) > ceiling:
                await self._reject(limit, scope, receive, send)
                return
            more = message.get("more_body", False)
        await self.app(scope, _ReplayReceive(bytes(body), receive), send)

    @staticmethod
    async def _reject(limit: int, scope: Any, receive: Any, send: Any) -> None:
        await JSONResponse(status_code=413, content={"detail": _too_large_detail(limit)})(scope, receive, send)


def _declared_content_length(scope: Any) -> int | None:
    for name, value in scope.get("headers", []):
        if name == b"content-length" and value.isdigit():
            return int(value)
    return None


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/extract")
async def extract(file: UploadFile = File(...)) -> dict[str, Any]:
    limit = _max_upload_bytes()
    # Bounded read: pull at most limit+1 bytes so we never materialise an
    # oversized body in memory; the spool / middleware bound the wire read.
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(status_code=413, detail=_too_large_detail(limit))
    filename = file.filename or "upload"
    try:
        # Parse in a disposable subprocess (timeout-kill + crash/OOM
        # isolation), off the event loop so concurrent requests proceed.
        payload = await asyncio.to_thread(run_isolated, data, filename, _PARSE_TIMEOUT_SECONDS)
    except ExtractionError as exc:
        # User-safe message (unsupported type / missing parser / bad input).
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ExtractionTimeout as exc:
        raise HTTPException(status_code=504, detail="extraction timed out") from exc
    except ExtractionCrash as exc:
        raise HTTPException(status_code=500, detail="extraction failed unexpectedly") from exc
    return payload


def create_app() -> FastAPI:
    app = FastAPI(title="agent-foundry file extractor")
    app.add_middleware(MaxBodySizeMiddleware)
    app.include_router(router)
    return app


def main() -> int:
    import uvicorn

    port = _env.read_int(_env.FILE_UPLOAD_PORT, fallback=8001) or 8001
    uvicorn.run(create_app(), host="localhost", port=port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
