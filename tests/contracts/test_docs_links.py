"""Internal documentation link checker (doc-architecture D5).

Every relative link in a tracked Markdown file must point at a file that
actually exists.  This is the guardrail that turns documentation drift
into a CI failure instead of a review burden -- it is the check that
would have caught the phantom ``bricks/channels/`` links that the old
``docs/README.md`` index carried.

Scope (deliberately narrow to stay low-noise and offline):

* Only **relative** links are checked.  External links (``http(s)://``,
  ``mailto:``) and pure in-page anchors (``#section``) are out of scope.
* Only files **tracked by git** are scanned (via ``git ls-files``), so
  build artifacts and local-only planning notes under ``.gitignore`` are
  ignored automatically.
* Link *targets* are checked for existence only; anchor fragments
  (``file.md#heading``) are not validated against headings.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# Inline links/images: the ``(target)`` half of ``[text](target)`` and
# ``![alt](target)``.  Reference definitions: ``[label]: target``.
_INLINE_LINK = re.compile(r"!?\[[^\]]*\]\(\s*<?([^)>\s]+)>?\s*(?:\"[^\"]*\")?\)")
_REF_DEF = re.compile(r"^\s*\[[^\]]+\]:\s*<?([^>\s]+)>?", re.MULTILINE)

# Targets that are not relative file paths and therefore out of scope.
_EXTERNAL = re.compile(r"^(?:[a-z][a-z0-9+.-]*:|//|#|mailto:)", re.IGNORECASE)


def _tracked_markdown_files() -> list[Path]:
    """Return every git-tracked ``*.md`` path, as absolute paths."""
    out = subprocess.run(
        ["git", "ls-files", "*.md"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    # ``git ls-files`` still lists files staged for deletion; skip any
    # that are gone from disk so a pending removal does not crash the scan.
    paths = (REPO_ROOT / line for line in out.stdout.splitlines() if line)
    return [p for p in paths if p.is_file()]


def _extract_targets(text: str) -> list[str]:
    """Pull every link/image target out of a Markdown document."""
    return _INLINE_LINK.findall(text) + _REF_DEF.findall(text)


def _is_relative_link(target: str) -> bool:
    """True for links that resolve to a file path in the repo."""
    return bool(target) and not _EXTERNAL.match(target)


def _resolve(md_file: Path, target: str) -> Path:
    """Resolve ``target`` (anchor/query stripped) against its source file."""
    path_part = target.split("#", 1)[0].split("?", 1)[0]
    base = REPO_ROOT if path_part.startswith("/") else md_file.parent
    return (base / path_part.lstrip("/")).resolve()


def _broken_links(md_file: Path) -> list[str]:
    """Return the relative-link targets in ``md_file`` that do not exist."""
    text = md_file.read_text(encoding="utf-8")
    targets = (t for t in _extract_targets(text) if _is_relative_link(t))
    return [t for t in targets if not _resolve(md_file, t).exists()]


@pytest.mark.parametrize(
    "md_file",
    _tracked_markdown_files(),
    ids=lambda p: str(p.relative_to(REPO_ROOT)),
)
def test_no_broken_internal_links(md_file: Path) -> None:
    """Fail on any dead relative link so doc drift cannot merge."""
    broken = _broken_links(md_file)
    rel = md_file.relative_to(REPO_ROOT)
    assert not broken, f"{rel} has dead relative link(s): " + ", ".join(broken)
