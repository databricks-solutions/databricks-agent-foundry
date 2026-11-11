"""Tests for the isolated file-extraction HTTP service (D6).

Exercises the request-shaped contract of ``extractor_service``: health,
the happy extraction path, the unsupported-format rejection (422), and
the hard size-cap rejection (413).  The extraction *logic* itself is
covered by ``payloads/agent/tests/unit/runtime/test_extraction.py``; here we
only assert the HTTP wrapper's guards and shape.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

# The service module imports FastAPI at module load; skip the whole file
# when the optional ``extraction`` extra (fastapi/httpx) is absent.
pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from payloads.agent.runtime.extractor_service import create_app  # noqa: E402


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def test_health_returns_ok(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_extract_csv_happy_path(client: TestClient) -> None:
    resp = client.post(
        "/extract",
        files={"file": ("data.csv", b"a,b\n1,2\n", "text/csv")},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["filename"] == "data.csv"
    assert body["format"] == "csv"
    assert body["truncated"] is False
    # CSV is rendered as a Markdown table.
    assert "| a | b |" in body["text"]


def test_extract_unsupported_format_returns_422(client: TestClient) -> None:
    resp = client.post(
        "/extract",
        files={"file": ("archive.zip", b"PK\x03\x04", "application/zip")},
    )
    assert resp.status_code == 422


def test_extract_oversized_file_returns_413(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FILE_UPLOAD_MAX_MB", "1")
    # 3 MB clears the 1 MB cap + 1 MB multipart slack, so the declared
    # Content-Length fast path rejects it before the body is parsed.
    oversized = b"x" * (3 * 1024 * 1024)
    resp = client.post(
        "/extract",
        files={"file": ("big.txt", oversized, "text/plain")},
    )
    assert resp.status_code == 413
    assert "cap" in resp.json()["detail"].lower()


def test_extract_oversized_chunked_body_returns_413(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Stream a large multipart body with no Content-Length (chunked) so the
    byte-counting receive -- not the header fast path -- does the rejecting."""
    monkeypatch.setenv("FILE_UPLOAD_MAX_MB", "1")
    boundary = "----foundrytest"
    preamble = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="big.txt"\r\n'
        "Content-Type: text/plain\r\n\r\n"
    ).encode()
    epilogue = f"\r\n--{boundary}--\r\n".encode()

    def stream_body() -> Iterator[bytes]:  # httpx uses chunked, no length
        yield preamble
        for _ in range(3):
            yield b"x" * (1024 * 1024)  # 3 MB total > 1 MB cap + 1 MB slack
        yield epilogue

    resp = client.post(
        "/extract",
        content=stream_body(),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    assert resp.status_code == 413
    assert "cap" in resp.json()["detail"].lower()
