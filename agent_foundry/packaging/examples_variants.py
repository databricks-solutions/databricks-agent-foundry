"""Discovery helpers for ``examples/`` init variants and wheel packaging.

Used by CI to ensure every on-disk variant is discoverable and that
``pyproject.toml`` ships all variant files via a single parent
``[tool.setuptools.package-data]`` entry for the ``examples`` package.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Final

_REPO_ROOT = Path(__file__).resolve().parents[2]
_EXAMPLES_DIR = _REPO_ROOT / "examples"

EXAMPLES_PACKAGE_DATA_GLOBS: Final[tuple[str, ...]] = (
    "**/*.yml",
    "**/*.yaml",
    "**/*.json",
    "**/*.md",
    "**/*.sql",
    "**/*.toml",
    "**/*.txt",
    "**/*.py",
    "*/assets/*.png",
    "*/assets/*.svg",
)
"""Globs pinned on the parent ``examples`` package in ``pyproject.toml``.

Rooted at ``examples/`` so every ``examples/<variant>/`` subpackage
tree is included without a per-variant ``package-data`` line.
"""

_MANIFEST_FILENAME = "manifest.toml"


def _package_data_table(data: dict) -> dict:
    """Return the ``[tool.setuptools.package-data]`` table from parsed pyproject."""
    tool = data.get("tool")
    if not isinstance(tool, dict):
        return {}
    setuptools = tool.get("setuptools")
    if not isinstance(setuptools, dict):
        return {}
    table = setuptools.get("package-data")
    return table if isinstance(table, dict) else {}


def discover_variant_dir_ids(*, examples_dir: Path | None = None) -> list[str]:
    """Return sorted variant ids for directories under ``examples/``.

    A directory qualifies when it contains both ``manifest.toml`` and
    ``__init__.py``.  Skips housekeeping dirs (``__pycache__``, names
    starting with ``_`` or ``.``).
    """
    root = examples_dir or _EXAMPLES_DIR
    ids: list[str] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        if child.name.startswith(("_", ".")):
            continue
        if not (child / _MANIFEST_FILENAME).is_file():
            continue
        if not (child / "__init__.py").is_file():
            continue
        ids.append(child.name)
    return ids


def read_examples_package_data_globs(pyproject_path: Path | None = None) -> list[str]:
    """Parse the ``\"examples\"`` package-data glob list from ``pyproject.toml``.

    Strips ``#`` comments line-by-line before parsing so inline
    documentation in the table does not break ``tomllib``.
    """
    path = pyproject_path or (_REPO_ROOT / "pyproject.toml")
    lines: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("#"):
            continue
        lines.append(re.sub(r"\s+#.*$", "", line))
    data = tomllib.loads("\n".join(lines))
    table = _package_data_table(data)
    globs = table.get("examples")
    if not isinstance(globs, list):
        raise ValueError(
            f'pyproject.toml [tool.setuptools.package-data] must define "examples" as a list of globs; got {globs!r}'
        )
    return [str(g) for g in globs]


def per_variant_package_data_keys(pyproject_path: Path | None = None) -> list[str]:
    """Return any legacy ``examples.<variant>`` keys still in package-data.

    New variants must not add per-variant entries; the parent ``examples``
    glob covers all subpackages.
    """
    path = pyproject_path or (_REPO_ROOT / "pyproject.toml")
    lines = [
        re.sub(r"\s+#.*$", "", line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("#")
    ]
    data = tomllib.loads("\n".join(lines))
    table = _package_data_table(data)
    return sorted(key for key in table if key.startswith("examples."))
