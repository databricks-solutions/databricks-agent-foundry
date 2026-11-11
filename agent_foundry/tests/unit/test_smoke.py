"""``agent_foundry.smoke``: probe aggregation, smoke.yml, adapter hook."""

from __future__ import annotations

import io
from pathlib import Path
from urllib.error import HTTPError

import pytest

from agent_foundry.smoke import (
    SmokeConfigError,
    SmokeFailure,
    SmokeProbe,
    SmokeQuestion,
    SmokeTarget,
    format_result,
    load_smoke_questions,
    normalize_host,
    probes_for,
    require_contains,
    require_text,
    run_probes,
)


def _raise(exc: Exception) -> str:
    raise exc


def _target(tmp_path: Path) -> SmokeTarget:
    return SmokeTarget(ws=None, app_url="https://a", workspace_host="https://w", instance_dir=tmp_path)


def test_run_probes_continues_after_a_failure() -> None:
    results = run_probes(
        [
            SmokeProbe("first", lambda: _raise(SmokeFailure("no text"))),
            SmokeProbe("second", lambda: "ok"),
        ]
    )
    assert [(r.name, r.ok) for r in results] == [("first", False), ("second", True)]
    assert format_result(results[0]) == "FAIL first: SmokeFailure: no text"
    assert format_result(results[1]) == "PASS second: ok"


def test_http_errors_report_status_and_body() -> None:
    error = HTTPError("https://a/invocations", 502, "Bad Gateway", {}, io.BytesIO(b"upstream down"))  # type: ignore[arg-type]
    (result,) = run_probes([SmokeProbe("invocations", lambda: _raise(error))])
    assert result.detail == "HTTP 502: upstream down"


def test_require_text_rejects_blank_and_previews() -> None:
    with pytest.raises(SmokeFailure, match="returned no text"):
        require_text("  \n ", "/invocations")
    assert require_text("hello\n  world", "x") == "hello world"


def test_normalize_host_adds_scheme() -> None:
    assert normalize_host("ws.example/") == "https://ws.example"
    assert normalize_host("https://ws.example") == "https://ws.example"


def test_smoke_questions_absent_file_means_none(tmp_path: Path) -> None:
    assert load_smoke_questions(tmp_path) == ()


def test_smoke_questions_are_read(tmp_path: Path) -> None:
    (tmp_path / "smoke.yml").write_text("questions:\n  - ' What is Unity Catalog? '\n", encoding="utf-8")
    assert load_smoke_questions(tmp_path) == (SmokeQuestion(question="What is Unity Catalog?"),)


def test_smoke_questions_may_expect_substrings(tmp_path: Path) -> None:
    body = "questions:\n  - question: Triage this\n    expect_contains: [TRIAGE-OK]\n  - Plain\n"
    (tmp_path / "smoke.yml").write_text(body, encoding="utf-8")
    assert load_smoke_questions(tmp_path) == (
        SmokeQuestion(question="Triage this", expect_contains=("TRIAGE-OK",)),
        SmokeQuestion(question="Plain"),
    )


def test_require_contains_names_missing_text() -> None:
    require_contains("ok TRIAGE-OK", ["TRIAGE-OK"], "probe")
    with pytest.raises(SmokeFailure, match="MISSING"):
        require_contains("ok TRIAGE-OK", ["TRIAGE-OK", "MISSING"], "probe")


@pytest.mark.parametrize(
    "body",
    [
        "questions: ['']\n",
        "question: [x]\n",
        "questions:\n  - question: x\n    expect_contains: ['']\n",
        "questions:\n  - question: x\n    expect: [y]\n",
    ],
)
def test_smoke_questions_reject_invalid_files(tmp_path: Path, body: str) -> None:
    (tmp_path / "smoke.yml").write_text(body, encoding="utf-8")
    with pytest.raises(SmokeConfigError, match="smoke.yml"):
        load_smoke_questions(tmp_path)


def test_adapter_without_hook_cannot_be_smoke_tested(tmp_path: Path) -> None:
    class _NoHook:
        name = "custom"

    with pytest.raises(SmokeConfigError, match="'custom' defines no smoke probes"):
        probes_for(_NoHook(), _target(tmp_path))


def test_adapter_returning_no_probes_is_an_error(tmp_path: Path) -> None:
    class _Empty:
        name = "empty"

        def smoke_probes(self, target: SmokeTarget) -> list[SmokeProbe]:
            return []

    with pytest.raises(SmokeConfigError, match="returned no smoke probes"):
        probes_for(_Empty(), _target(tmp_path))


def test_builtin_adapters_provide_smoke_probes() -> None:
    from agent_foundry.deploy.payload_adapters import get_payload_adapter
    from agent_foundry.smoke import SmokeCapable

    for name in ("agent", "rest_mcp"):
        assert isinstance(get_payload_adapter(name), SmokeCapable), name
