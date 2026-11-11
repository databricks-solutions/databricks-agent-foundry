"""Lock-derived App constraint files stay in sync with uv.lock."""

from __future__ import annotations

from agent_foundry.constraints import (
    APP_CONSTRAINT_EXTRAS,
    forbidden_snippets_in,
    load_constraint_text,
    package_filename,
)
from agent_foundry.constraints.export import export_extra, repo_root_from, sanitize_export


def test_sanitize_drops_index_and_hash_lines() -> None:
    raw = "\n".join(
        [
            "--index-url https://pypi.org/simple",
            "--extra-index-url https://example.invalid/simple",
            "openai-agents==0.15.3",
            "foo==1 --hash=sha256:deadbeef",
        ]
    )
    out = sanitize_export(raw)
    assert "openai-agents==0.15.3" in out
    assert forbidden_snippets_in(out) == []
    assert "foo==1" not in out  # whole line dropped because of --hash=


def test_committed_files_have_no_forbidden_snippets() -> None:
    for extra in APP_CONSTRAINT_EXTRAS:
        text = load_constraint_text(extra)
        leftover = forbidden_snippets_in(text)
        assert leftover == [], f"{extra}: {leftover}"


def test_committed_files_match_uv_export() -> None:
    """CI freshness gate: after ``uv lock``, re-run the exporter."""
    root = repo_root_from()
    for extra in APP_CONSTRAINT_EXTRAS:
        expected = export_extra(extra, root=root)
        committed = (root / "agent_foundry" / "constraints" / package_filename(extra)).read_text(encoding="utf-8")
        assert committed == expected, (
            f"{package_filename(extra)} is stale. Run "
            "`python -m agent_foundry.constraints.export` and commit the result."
        )


def test_runtime_constraints_pin_openai_agents() -> None:
    text = load_constraint_text("runtime")
    assert "openai-agents==" in text
    assert "protobuf==" in text
