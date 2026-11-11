"""Tests for the out-of-process file-extraction worker (§7 hardening).

``run_isolated`` is exercised against a real ``spawn``ed worker for the
result / rejection / timeout contract; the crash mapping, the in-child
``_worker`` outcomes, and the per-child hardening steps are tested
without a subprocess (or in a throwaway interpreter where the step
mutates global state).
"""

from __future__ import annotations

import multiprocessing
import os
import queue
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from payloads.agent.runtime import extract_worker
from payloads.agent.runtime.extract_worker import (
    ExtractionCrash,
    ExtractionTimeout,
    run_isolated,
)
from payloads.agent.runtime.extraction import ExtractionError

_REPO_ROOT = Path(__file__).resolve().parents[5]
_MEM_LIMIT_VAR = "FILE_UPLOAD_MEM_LIMIT_MB"
_MIB = 1024 * 1024

_BILLION_LAUGHS = (
    '<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;">]><lolz>&lol2;</lolz>'
)

# Runs in a fresh interpreter: ``defuse_stdlib()`` patches the stdlib
# process-wide, which must not leak into the rest of the test session.
_HARDENED_PARSE_SCRIPT = """
import sys
import xml.etree.ElementTree as ET
from payloads.agent.runtime.extract_worker import _harden_xml

_harden_xml()
try:
    ET.fromstring(sys.stdin.read())
except Exception as exc:
    print(type(exc).__name__)
else:
    print("parsed")
"""


class _SilentProcess:
    """A worker that exits immediately without reporting a result."""

    def __init__(self, target: Any, args: tuple[Any, ...]) -> None:
        self.target = target
        self.args = args

    def start(self) -> None:
        return None

    def join(self, timeout: float | None = None) -> None:
        return None

    def is_alive(self) -> bool:
        return False


class _SilentContext:
    Process = _SilentProcess

    @staticmethod
    def Queue() -> queue.Queue[Any]:  # noqa: N802 - mirrors multiprocessing API
        return queue.Queue()


@pytest.fixture
def no_child_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop ``_worker`` from hardening / rlimiting the test process itself."""
    monkeypatch.setattr(extract_worker, "_apply_memory_ceiling", lambda: None)
    monkeypatch.setattr(extract_worker, "_harden_xml", lambda: None)


class TestRunIsolated:
    def test_returns_extracted_payload(self) -> None:
        result = run_isolated(b"a,b\n1,2\n", "data.csv", timeout=60)
        assert result["format"] == "csv"
        assert "| a | b |" in result["text"]

    def test_rejection_crosses_process_boundary(self) -> None:
        with pytest.raises(ExtractionError, match="unsupported"):
            run_isolated(b"...", "image.png", timeout=60)

    def test_overrun_is_killed_and_raises_timeout(self) -> None:
        with pytest.raises(ExtractionTimeout):
            run_isolated(b"a,b\n1,2\n", "data.csv", timeout=0)
        assert multiprocessing.active_children() == []

    def test_worker_exit_without_result_raises_crash(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(extract_worker, "_MP_CONTEXT", _SilentContext())
        with pytest.raises(ExtractionCrash):
            run_isolated(b"a,b\n1,2\n", "data.csv", timeout=60)


@pytest.mark.usefixtures("no_child_setup")
class TestWorker:
    def test_success_reports_ok(self) -> None:
        results: queue.Queue[Any] = queue.Queue()
        extract_worker._worker(b"a,b\n1,2\n", "data.csv", results)
        status, payload = results.get_nowait()
        assert status == "ok"
        assert payload["format"] == "csv"

    def test_extraction_error_reports_rejected(self) -> None:
        results: queue.Queue[Any] = queue.Queue()
        extract_worker._worker(b"...", "image.png", results)
        status, message = results.get_nowait()
        assert status == "rejected"
        assert "unsupported" in message

    def test_unexpected_error_reports_generic_crash(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def explode(data: bytes, filename: str) -> None:
            raise RuntimeError("internal detail /etc/passwd")

        monkeypatch.setattr(extract_worker, "extract_file", explode)
        results: queue.Queue[Any] = queue.Queue()
        extract_worker._worker(b"a,b\n1,2\n", "data.csv", results)
        assert results.get_nowait() == ("crashed", "could not extract this file")


class TestHardenXml:
    def test_entity_expansion_is_rejected_after_hardening(self) -> None:
        pytest.importorskip("defusedxml")
        completed = subprocess.run(
            [sys.executable, "-c", _HARDENED_PARSE_SCRIPT],
            input=_BILLION_LAUGHS,
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
            cwd=_REPO_ROOT,
            env={**os.environ, "PYTHONPATH": str(_REPO_ROOT)},
        )
        assert completed.stdout.strip() == "EntitiesForbidden"

    def test_noop_without_defusedxml(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "defusedxml", None)
        extract_worker._harden_xml()


class TestMemoryCeiling:
    @pytest.fixture
    def setrlimit_calls(self, monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, tuple[int, int]]]:
        resource = pytest.importorskip("resource")
        calls: list[tuple[int, tuple[int, int]]] = []
        monkeypatch.setattr(resource, "setrlimit", lambda which, limits: calls.append((which, limits)))
        return calls

    def test_unset_leaves_limits_alone(
        self, monkeypatch: pytest.MonkeyPatch, setrlimit_calls: list[tuple[int, tuple[int, int]]]
    ) -> None:
        monkeypatch.delenv(_MEM_LIMIT_VAR, raising=False)
        extract_worker._apply_memory_ceiling()
        assert setrlimit_calls == []

    @pytest.mark.parametrize(
        ("hard_mib", "expected_mib"),
        [(None, 64), (32, 32)],
        ids=["unlimited-hard", "lower-hard-wins"],
    )
    def test_caps_address_space_within_hard_limit(
        self,
        monkeypatch: pytest.MonkeyPatch,
        setrlimit_calls: list[tuple[int, tuple[int, int]]],
        hard_mib: int | None,
        expected_mib: int,
    ) -> None:
        import resource

        hard = resource.RLIM_INFINITY if hard_mib is None else hard_mib * _MIB
        monkeypatch.setenv(_MEM_LIMIT_VAR, "64")
        monkeypatch.setattr(resource, "getrlimit", lambda which: (hard, hard))
        extract_worker._apply_memory_ceiling()
        assert setrlimit_calls == [(resource.RLIMIT_AS, (expected_mib * _MIB, hard))]
