"""Tests for :mod:`agent_foundry._meta`.

The framework-SHA discovery helper is the foundation the
``agent-foundry init`` wizard (issue #210) uses to stamp the running
CLI's exact build into the generated downstream repo's
``pyproject.toml``.  Edge cases beyond the primary install paths
(``uvx`` / ``uv tool install``) are deliberately out of scope -- see
issue #211 for the deferred list.
"""

from __future__ import annotations

import json
from importlib import metadata as _metadata
from typing import Any

import pytest

from agent_foundry._meta import DiscoveryResult, discover_framework_sha


class _FakeDistribution:
    """Minimal stand-in for :class:`importlib.metadata.Distribution`.

    We only need :meth:`read_text` to satisfy
    :func:`discover_framework_sha` -- the real ``Distribution`` API
    has many more methods but they are not exercised by the helper.
    """

    def __init__(self, payload: str | None) -> None:
        self._payload = payload

    def read_text(self, filename: str) -> str | None:
        assert filename == "direct_url.json", f"unexpected metadata file requested: {filename!r}"
        return self._payload


@pytest.fixture
def patched_distribution(monkeypatch: pytest.MonkeyPatch):
    """Helper to swap :func:`importlib.metadata.distribution`."""

    def _patch(direct_url: dict[str, Any] | str | None) -> None:
        if direct_url is None:
            payload: str | None = None
        elif isinstance(direct_url, str):
            payload = direct_url
        else:
            payload = json.dumps(direct_url)

        def _fake(name: str) -> _FakeDistribution:
            assert name == "agent-foundry"
            return _FakeDistribution(payload)

        monkeypatch.setattr("agent_foundry._meta._metadata.distribution", _fake)

    return _patch


def test_discover_returns_sha_for_uvx_install(patched_distribution) -> None:
    """The primary supported install path -- ``uvx --from
    "agent-foundry @ git+ssh://...@<sha>" ...`` -- writes a
    ``direct_url.json`` with ``vcs_info.commit_id`` set to the
    resolved SHA.  Discovery must return that SHA verbatim.
    """
    patched_distribution(
        {
            "url": "git+ssh://git@github.com/example/agent-foundry.git",
            "vcs_info": {
                "vcs": "git",
                "commit_id": "abc123def456",
                "requested_revision": "abc123def456",
            },
        },
    )
    result = discover_framework_sha()
    assert result == DiscoveryResult(
        sha="abc123def456",
        requested_ref="abc123def456",
        reason="",
    )


def test_discover_captures_requested_branch_ref(patched_distribution) -> None:
    """When the user installed from a branch (``@main``), the resolved
    commit lands in ``commit_id`` and the original ref lands in
    ``requested_revision``.  Both must be exposed so the caller can
    warn that the generated repo is being pinned to a moving target.
    See issue #211 for the deferred follow-up.
    """
    patched_distribution(
        {
            "url": "git+ssh://git@github.com/example/agent-foundry.git",
            "vcs_info": {
                "vcs": "git",
                "commit_id": "0123456789abcdef",
                "requested_revision": "main",
            },
        },
    )
    result = discover_framework_sha()
    assert result.sha == "0123456789abcdef"
    assert result.requested_ref == "main"
    assert result.reason == ""


def test_discover_returns_none_when_direct_url_missing(patched_distribution) -> None:
    """Wheel / sdist / PyPI installs have no ``direct_url.json``.  The
    helper must surface that with a non-empty ``reason`` so the
    caller can decide between hard error and fallback.
    """
    patched_distribution(None)
    result = discover_framework_sha()
    assert result.sha is None
    assert "direct_url.json missing" in result.reason


def test_discover_returns_none_when_no_vcs_info(patched_distribution) -> None:
    """Editable installs (`pip install -e .`) record a path-based
    ``direct_url.json`` with ``dir_info`` but no ``vcs_info``.
    Discovery returns ``sha=None`` with a descriptive reason that
    flags the editable shape so the wizard can point the user at
    ``--framework-ref`` (#211).
    """
    patched_distribution(
        {
            "url": "file:///Users/me/checkouts/agent-foundry",
            "dir_info": {"editable": True},
        },
    )
    result = discover_framework_sha()
    assert result.sha is None
    assert "editable" in result.reason
    assert "--framework-ref" in result.reason


def test_discover_returns_none_for_non_editable_local_path(patched_distribution) -> None:
    """Local-path (non-editable) installs land in the same fallback
    bucket as editable but warrant a distinct reason so the warning
    is actionable (#211).
    """
    patched_distribution(
        {
            "url": "file:///Users/me/checkouts/agent-foundry",
            "dir_info": {},
        },
    )
    result = discover_framework_sha()
    assert result.sha is None
    assert "local path" in result.reason
    assert "--framework-ref" in result.reason


def test_discover_returns_none_with_generic_reason_when_no_dir_info(
    patched_distribution,
) -> None:
    """Direct_url.json with neither ``vcs_info`` nor ``dir_info``
    (e.g. a future archive install shape) falls back to the generic
    reason but still mentions the ``--framework-ref`` escape hatch.
    """
    patched_distribution({"url": "https://example.com/agent-foundry.tar.gz"})
    result = discover_framework_sha()
    assert result.sha is None
    assert "--framework-ref" in result.reason


def test_discover_handles_invalid_json(patched_distribution) -> None:
    """A corrupt ``direct_url.json`` should not crash the helper --
    return ``sha=None`` with a reason mentioning the JSON error so
    debug output is actionable.
    """
    patched_distribution("not-json{")
    result = discover_framework_sha()
    assert result.sha is None
    assert "not valid JSON" in result.reason


def test_discover_handles_missing_distribution(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the framework is imported from a source checkout that was
    never installed, ``importlib.metadata.distribution()`` raises
    :class:`PackageNotFoundError`.  Discovery must catch that and
    return ``sha=None`` rather than propagating.
    """

    def _raise(name: str) -> Any:
        raise _metadata.PackageNotFoundError(name)

    monkeypatch.setattr("agent_foundry._meta._metadata.distribution", _raise)
    result = discover_framework_sha()
    assert result.sha is None
    assert "not found" in result.reason
