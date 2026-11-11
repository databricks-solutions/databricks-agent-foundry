"""Tests for the pinned-SHA comparison helpers in :mod:`agent_foundry._meta`.

These guard against the failure mode where ``agent-foundry deploy`` runs
from a framework build that differs from the SHA the downstream repo
pins in ``pyproject.toml`` -- most commonly a globally-installed CLI
shadowing the project ``.venv``. Because ``deploy`` bundles the *running*
build into the app wheel, such a mismatch silently ships the wrong
framework, so both ``agent-foundry validate`` and the deploy path surface
it via :func:`agent_foundry._meta.compare_pinned_sha`.
"""

from __future__ import annotations

import json
from importlib import metadata as _metadata
from pathlib import Path
from typing import Any

import pytest

from agent_foundry._meta import (
    compare_pinned_sha,
    read_pinned_framework_ref,
)

_GIT_URL = "git+https://host/org/repo.git"


def _write_pyproject(tmp_path: Path, dependencies: list[str]) -> Path:
    """Write a minimal ``pyproject.toml`` with the given dependencies and
    return the repo root (``tmp_path``)."""
    deps = ",\n    ".join(json.dumps(d) for d in dependencies)
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "x"\nversion = "0.0.0"\ndependencies = [\n    {deps}\n]\n',
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture
def patch_running_build(monkeypatch: pytest.MonkeyPatch):
    """Swap ``importlib.metadata.distribution`` so ``discover_framework_sha``
    reports a controllable ``direct_url.json``."""

    class _FakeDistribution:
        def __init__(self, payload: str | None) -> None:
            self._payload = payload

        def read_text(self, filename: str) -> str | None:
            assert filename == "direct_url.json"
            return self._payload

    def _patch(direct_url: dict[str, Any] | None) -> None:
        payload = None if direct_url is None else json.dumps(direct_url)

        def _fake(name: str) -> _FakeDistribution:
            assert name == "agent-foundry"
            return _FakeDistribution(payload)

        monkeypatch.setattr("agent_foundry._meta._metadata.distribution", _fake)

    return _patch


def _vcs(commit_id: str, requested_revision: str) -> dict[str, Any]:
    return {
        "url": _GIT_URL,
        "vcs_info": {
            "vcs": "git",
            "commit_id": commit_id,
            "requested_revision": requested_revision,
        },
    }


# ---------------------------------------------------------------------------
# read_pinned_framework_ref
# ---------------------------------------------------------------------------


def test_read_pin_parses_https_url_with_extras(tmp_path: Path) -> None:
    root = _write_pyproject(
        tmp_path,
        [f"agent-foundry[runtime,eval,dev] @ {_GIT_URL}@abc123def456", "pyyaml>=6"],
    )
    assert read_pinned_framework_ref(root / "pyproject.toml") == "abc123def456"


def test_read_pin_parses_ssh_url_with_user_segment(tmp_path: Path) -> None:
    # The ``git@host`` user segment must not be mistaken for the ref.
    root = _write_pyproject(
        tmp_path,
        ["agent-foundry @ git+ssh://git@github.com/org/repo.git@v1.2.3"],
    )
    assert read_pinned_framework_ref(root / "pyproject.toml") == "v1.2.3"


def test_read_pin_returns_none_when_not_pinned_to_ref(tmp_path: Path) -> None:
    # A git URL with no explicit ``@<ref>`` must not mis-parse the path.
    root = _write_pyproject(tmp_path, ["agent-foundry @ git+ssh://git@github.com/org/repo.git"])
    assert read_pinned_framework_ref(root / "pyproject.toml") is None


def test_read_pin_returns_none_when_absent(tmp_path: Path) -> None:
    root = _write_pyproject(tmp_path, ["pyyaml>=6", "databricks-sdk>=0.20"])
    assert read_pinned_framework_ref(root / "pyproject.toml") is None


def test_read_pin_returns_none_when_pyproject_missing(tmp_path: Path) -> None:
    assert read_pinned_framework_ref(tmp_path / "pyproject.toml") is None


# ---------------------------------------------------------------------------
# compare_pinned_sha
# ---------------------------------------------------------------------------


def test_compare_matches_exact_sha(tmp_path: Path, patch_running_build) -> None:
    sha = "45daebae0f0d3a7e0c3bcf454d446c274aa5eb44"
    root = _write_pyproject(tmp_path, [f"agent-foundry @ {_GIT_URL}@{sha}"])
    patch_running_build(_vcs(sha, sha))
    result = compare_pinned_sha(root)
    assert result.matches is True
    assert result.skipped is False


def test_compare_matches_abbreviated_sha(tmp_path: Path, patch_running_build) -> None:
    full = "45daebae0f0d3a7e0c3bcf454d446c274aa5eb44"
    root = _write_pyproject(tmp_path, [f"agent-foundry @ {_GIT_URL}@45daebae"])
    patch_running_build(_vcs(full, "45daebae"))
    assert compare_pinned_sha(root).matches is True


def test_compare_matches_tag_via_requested_revision(tmp_path: Path, patch_running_build) -> None:
    root = _write_pyproject(tmp_path, [f"agent-foundry @ {_GIT_URL}@v0.3.0"])
    patch_running_build(_vcs("0123456789abcdef0123456789abcdef01234567", "v0.3.0"))
    assert compare_pinned_sha(root).matches is True


def test_compare_flags_mismatch(tmp_path: Path, patch_running_build) -> None:
    root = _write_pyproject(tmp_path, [f"agent-foundry @ {_GIT_URL}@aaaaaaaaaaaaaaaaaaaa"])
    patch_running_build(_vcs("bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "bbbbbbbb"))
    result = compare_pinned_sha(root)
    assert result.matches is False
    assert result.skipped is False
    assert "aaaaaaaa" in result.message and "bbbbbbbb" in result.message
    assert "uv run agent-foundry" in result.message


def test_compare_skips_when_not_pinned(tmp_path: Path, patch_running_build) -> None:
    root = _write_pyproject(tmp_path, ["pyyaml>=6"])
    patch_running_build(_vcs("cccccccccccccccccccc", "cccccccc"))
    result = compare_pinned_sha(root)
    assert result.matches is True
    assert result.skipped is True


def test_compare_skips_when_running_build_has_no_sha(tmp_path: Path, patch_running_build) -> None:
    # Editable / local / wheel installs cannot be compared -> benign skip.
    root = _write_pyproject(tmp_path, [f"agent-foundry @ {_GIT_URL}@abc123"])
    patch_running_build({"url": "file:///checkout", "dir_info": {"editable": True}})
    result = compare_pinned_sha(root)
    assert result.matches is True
    assert result.skipped is True


def test_compare_skips_when_distribution_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _write_pyproject(tmp_path, [f"agent-foundry @ {_GIT_URL}@abc123"])

    def _raise(name: str) -> Any:
        raise _metadata.PackageNotFoundError(name)

    monkeypatch.setattr("agent_foundry._meta._metadata.distribution", _raise)
    result = compare_pinned_sha(root)
    assert result.matches is True
    assert result.skipped is True
