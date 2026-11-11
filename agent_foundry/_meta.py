"""Framework self-introspection helpers.

Internal utility module for code paths that need to know *which* build
of ``agent-foundry`` is currently running -- most notably the
``agent-foundry init`` wizard (issue #210), which must stamp the SHA
of the running CLI into the generated downstream repo's
``pyproject.toml`` so that the new repo pins the *same* framework
build the user just bootstrapped from.

The information lives in the installed distribution's
``direct_url.json`` file (PEP 610), populated by ``pip`` / ``uv`` when
a package is installed from a VCS URL.  Two install shapes are
supported as first-class:

* ``uvx --from "agent-foundry @ git+ssh://...@<sha>" agent-foundry ...``
* ``uv tool install "agent-foundry @ git+ssh://...@<sha>"``

Other install shapes fall back to ``DiscoveryResult(sha=None,
requested_ref="main", reason=...)`` and the caller decides whether
that warrants a hard error, a warning, or a silent default.  See
issue #211 for the deferred edge-case work (editable installs, sdist
installs, branch refs that have not been resolved to a commit).
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass
from importlib import metadata as _metadata
from pathlib import Path

_DIST_NAME = "agent-foundry"


@dataclass(frozen=True)
class DiscoveryResult:
    """Outcome of inspecting the framework's installed distribution.

    Attributes:
        sha: The resolved git commit id the framework was installed
            from, or ``None`` if it could not be determined.
        requested_ref: The ref the user *asked* ``pip`` / ``uv`` to
            install (e.g. ``"main"``, ``"v0.3.0"``, a SHA).  Empty
            string when the install record carries no VCS metadata at
            all.
        reason: Human-readable explanation when ``sha`` is ``None``.
            Suitable for inclusion in a ``warnings.warn`` payload.
    """

    sha: str | None
    requested_ref: str
    reason: str


def discover_framework_sha() -> DiscoveryResult:
    """Best-effort lookup of the framework's installed git SHA.

    Returns:
        A :class:`DiscoveryResult`.  The ``sha`` attribute is
        non-``None`` only when the framework was installed from a VCS
        source and ``pip`` / ``uv`` recorded the resolved commit in
        ``direct_url.json``.  All other install shapes (editable,
        sdist, wheel) yield ``sha=None`` with a descriptive ``reason``
        so the caller can decide between hard error, warning, or
        silent default.
    """
    try:
        dist = _metadata.distribution(_DIST_NAME)
    except _metadata.PackageNotFoundError:
        return DiscoveryResult(
            sha=None,
            requested_ref="",
            reason=(
                f"distribution {_DIST_NAME!r} not found in the current "
                "environment -- agent-foundry must be installed (not just "
                "imported from a source checkout) for SHA discovery to work."
            ),
        )

    raw = dist.read_text("direct_url.json")
    if raw is None:
        return DiscoveryResult(
            sha=None,
            requested_ref="",
            reason=(
                "direct_url.json missing from the installed distribution -- "
                "agent-foundry was probably installed from a wheel / sdist / "
                "PyPI without VCS metadata. No SHA available."
            ),
        )

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return DiscoveryResult(
            sha=None,
            requested_ref="",
            reason=f"direct_url.json is not valid JSON: {exc}",
        )

    vcs_info = payload.get("vcs_info") or {}
    sha = vcs_info.get("commit_id") or None
    requested_ref = vcs_info.get("requested_revision") or ""

    if sha is None:
        return DiscoveryResult(
            sha=None,
            requested_ref=requested_ref,
            reason=_explain_missing_vcs(payload),
        )
    return DiscoveryResult(sha=sha, requested_ref=requested_ref, reason="")


def _explain_missing_vcs(payload: dict[str, object]) -> str:
    """Build a tailored ``reason`` for installs without a resolved SHA.

    The three sub-cases the wizard cares about -- editable checkouts,
    non-editable local paths, and "something else with no vcs_info" --
    each get a distinct, actionable message so the developer running
    ``agent-foundry init`` knows the cleanest override path.  See
    #211 for the deferred edge-case background.
    """
    dir_info = payload.get("dir_info")
    url = payload.get("url")
    url_str = url if isinstance(url, str) else ""
    suggestion = (
        "Pass ``--framework-ref <sha>`` to ``agent-foundry init`` to "
        "pin the generated repo to a specific commit instead of "
        "``main``."
    )

    if isinstance(dir_info, dict) and dir_info.get("editable"):
        location = f" from {url_str!r}" if url_str else ""
        return f"agent-foundry is installed in editable mode{location}; no commit SHA is available. {suggestion}"
    if isinstance(dir_info, dict):
        location = f" from {url_str!r}" if url_str else ""
        return f"agent-foundry was installed from a local path{location}; no commit SHA is available. {suggestion}"
    return f"direct_url.json has no vcs_info.commit_id -- the install source is not a recognised VCS URL. {suggestion}"


@dataclass(frozen=True)
class PinComparison:
    """Outcome of comparing the *running* framework build against the SHA
    a downstream repo pins in its ``pyproject.toml``.

    ``deploy`` bundles the *running* ``agent-foundry`` into the app wheel
    (see :mod:`agent_foundry.deploy.staging`), so a mismatch means the
    deployed artifact would not match what the repo pins -- typically a
    globally-installed CLI shadowing the project's ``.venv``.

    Attributes:
        matches: ``True`` when the running build matches the pin, or when
            the comparison was skipped (nothing actionable to report).
        skipped: ``True`` when no comparison could be made (repo does not
            pin agent-foundry to a git ref, or the running build has no
            resolvable VCS SHA -- editable / local / wheel install).
        pinned_ref: The ref pinned in ``pyproject.toml`` (SHA / tag /
            branch), or ``None``.
        running_sha: The resolved commit id of the running build, or
            ``None``.
        message: Human-readable summary suitable for a warning payload or
            a validate ``CheckResult`` detail.
    """

    matches: bool
    skipped: bool
    pinned_ref: str | None
    running_sha: str | None
    message: str


def read_pinned_framework_ref(pyproject_path: Path) -> str | None:
    """Return the git ref ``agent-foundry`` is pinned to in a downstream
    repo's ``pyproject.toml`` ``[project].dependencies``, or ``None``.

    Recognises the shape the ``init`` wizard generates, e.g.::

        agent-foundry[runtime,eval,dev] @ git+https://host/org/repo.git@<ref>

    Returns ``None`` when the file is unreadable, agent-foundry is absent,
    or it is not pinned to a ``git+`` URL with an explicit ref.
    """
    try:
        data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    deps = (data.get("project") or {}).get("dependencies") or []
    for dep in deps:
        if isinstance(dep, str) and _requirement_name(dep) == _DIST_NAME:
            return _extract_vcs_ref(dep)
    return None


def _requirement_name(dep: str) -> str:
    """Normalise the distribution name from a PEP 508 requirement string
    (strip extras, whitespace, and canonicalise ``_``/case)."""
    head = dep.split("@", 1)[0]
    head = head.split("[", 1)[0]
    return head.strip().replace("_", "-").lower()


def _extract_vcs_ref(dep: str) -> str | None:
    """Extract the ``@<ref>`` a ``git+`` requirement is pinned to.

    Guards against the ``git+ssh://git@host/...`` user segment and
    ref-less URLs (which would otherwise mis-parse the host or path as a
    ref)."""
    _, _, url = dep.partition("@")
    url = url.strip().split("#", 1)[0].strip()
    if not url.startswith("git+") or "@" not in url:
        return None
    ref = url.rsplit("@", 1)[1].strip()
    if not ref or "://" in ref or ref.endswith(".git"):
        return None
    return ref


def _short(ref: str) -> str:
    return ref if len(ref) <= 12 else ref[:12]


def _refs_match(pinned: str, sha: str, requested_ref: str) -> bool:
    """True when *pinned* refers to the same build as the running install.

    Handles full SHAs, abbreviated SHAs (prefix match either direction),
    and tag/branch pins (compared against the install's
    ``requested_revision``)."""
    for candidate in (sha, requested_ref):
        if candidate and (pinned == candidate or candidate.startswith(pinned) or pinned.startswith(candidate)):
            return True
    return False


def compare_pinned_sha(project_root: Path) -> PinComparison:
    """Compare the running ``agent-foundry`` build against the repo's pin.

    See :class:`PinComparison` for the semantics of the returned fields.
    Never raises: unreadable configs and unresolvable installs degrade to
    ``skipped=True`` so callers can treat "cannot compare" as benign.
    """
    pinned = read_pinned_framework_ref(project_root / "pyproject.toml")
    if pinned is None:
        return PinComparison(
            matches=True,
            skipped=True,
            pinned_ref=None,
            running_sha=None,
            message="agent-foundry is not pinned to a git ref in pyproject.toml; SHA check skipped.",
        )

    running = discover_framework_sha()
    if running.sha is None:
        return PinComparison(
            matches=True,
            skipped=True,
            pinned_ref=pinned,
            running_sha=None,
            message=(
                f"repo pins {_short(pinned)} but the running build has no resolvable VCS SHA "
                f"({running.reason}); SHA check skipped."
            ),
        )

    if _refs_match(pinned, running.sha, running.requested_ref):
        return PinComparison(
            matches=True,
            skipped=False,
            pinned_ref=pinned,
            running_sha=running.sha,
            message=f"running build {_short(running.sha)} matches the pinned ref {_short(pinned)}.",
        )

    return PinComparison(
        matches=False,
        skipped=False,
        pinned_ref=pinned,
        running_sha=running.sha,
        message=(
            f"pinned ref {_short(pinned)} != running build {_short(running.sha)}. "
            f"`agent-foundry deploy` bundles the running build into the app wheel, so the deploy "
            f"would ship a framework that does not match pyproject.toml. This usually means a "
            f"globally-installed agent-foundry is shadowing the project venv. Fix: run `uv sync` "
            f"and invoke deploy via `uv run agent-foundry` from the project root."
        ),
    )


__all__ = [
    "DiscoveryResult",
    "PinComparison",
    "compare_pinned_sha",
    "discover_framework_sha",
    "read_pinned_framework_ref",
]
