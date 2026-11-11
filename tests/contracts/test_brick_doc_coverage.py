"""Per-brick documentation coverage (doc-architecture D6 / §5.3).

Every brick that ships in the wheel must carry exactly one authored
`README.md`, and the doc indexes must list exactly those bricks. "Ships
in the wheel" is defined the same way ``setuptools`` defines it: the
brick directory is an importable package, i.e. it has an ``__init__.py``
(see ``[tool.setuptools.packages.find]`` in ``pyproject.toml``).

This is the guardrail that would have caught the ``channels`` ghost and
the ``app``/``agent_app`` mismatch the old indexes carried: a brick can
no longer be added to the wheel without an authored doc, and a doc can no
longer linger for a brick that was dropped from the wheel.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BRICKS_ROOT = REPO_ROOT / "bricks"

# Doc index tables link each brick to ``bricks/<key>/README.md`` (with a
# leading ``../`` depth that varies by file). Pulling the keys out of those
# links is table-structure-agnostic and ignores every other table on the page.
_BRICK_LINK = re.compile(r"bricks/([A-Za-z0-9_]+)/README\.md")

# Markdown indexes whose brick tables must enumerate exactly the wheel bricks.
_BRICK_INDEX_DOCS = {
    "docs/index.md": REPO_ROOT / "docs" / "index.md",
    "docs/reference/bricks.md": REPO_ROOT / "docs" / "reference" / "bricks.md",
}


def _brick_dirs() -> list[Path]:
    """Every immediate subdirectory of ``bricks/`` (excluding caches)."""
    return sorted(d for d in BRICKS_ROOT.iterdir() if d.is_dir() and d.name != "__pycache__")


def _wheel_brick_dirs() -> list[Path]:
    """Brick directories that ship in the wheel (importable packages)."""
    return [d for d in _brick_dirs() if (d / "__init__.py").is_file()]


def _wheel_brick_names() -> set[str]:
    return {d.name for d in _wheel_brick_dirs()}


@pytest.mark.parametrize("brick_dir", _wheel_brick_dirs(), ids=lambda p: p.name)
def test_wheel_brick_has_readme(brick_dir: Path) -> None:
    """A wheel-shipping brick must have exactly one authored README."""
    readme = brick_dir / "README.md"
    assert readme.is_file(), (
        f"brick {brick_dir.name!r} ships in the wheel (has __init__.py) but has "
        "no bricks/{0}/README.md. Author the brick's single doc.".format(brick_dir.name)
    )


@pytest.mark.parametrize("rel", sorted(_BRICK_INDEX_DOCS), ids=lambda r: r)
def test_brick_index_lists_every_wheel_brick(rel: str) -> None:
    """The brick tables in the doc map and the brick reference must list
    exactly the bricks that ship in the wheel -- no missing entries, no
    stale ones. Keeps the two hand-maintained indexes from drifting apart
    or away from the actual brick set."""
    documented = set(_BRICK_LINK.findall(_BRICK_INDEX_DOCS[rel].read_text(encoding="utf-8")))
    expected = _wheel_brick_names()
    missing = expected - documented
    stale = documented - expected
    assert not missing and not stale, (
        f"{rel} brick index is out of sync with the wheel bricks.\n"
        f"  Missing: {sorted(missing) or 'none'}\n"
        f"  Stale (listed but not a wheel brick): {sorted(stale) or 'none'}\n"
        "Update the brick table to list exactly the bricks that ship in the wheel."
    )
