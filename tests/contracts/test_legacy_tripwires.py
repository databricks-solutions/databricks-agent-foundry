"""Tripwires against reintroducing removed legacy paths (issue #589).

Two scans over the shipped sources (``agent_foundry``, ``bricks``,
``payloads``, ``examples``; test directories excluded):

* **Legacy layout references.** No file may name ``instance.yml``,
  ``conf/targets`` or ``agent/config.yml``.  :mod:`agent_foundry._legacy_layout`
  is the one allowlisted module: it detects leftover files and fails fast.
* **Deprecation emission.** No module may emit a deprecation warning,
  neither through ``warnings.warn(..., DeprecationWarning)`` (or the
  ``Pending``/``Future`` variants) nor through a string literal carrying
  ``DeprecationWarning`` text that is echoed instead.  A new deprecation
  window must be added to :data:`DEPRECATION_ALLOWLIST` with a reason.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

SCAN_ROOTS: tuple[Path, ...] = (
    REPO_ROOT / "agent_foundry",
    REPO_ROOT / "bricks",
    REPO_ROOT / "payloads",
    REPO_ROOT / "examples",
)

EXCLUDED_DIRS = frozenset({"tests", "__pycache__", "node_modules", ".venv"})

TEXT_SUFFIXES = frozenset({".py", ".yml", ".yaml", ".toml", ".md", ".json", ".sql", ".ts", ".tsx", ".txt", ".svg"})

LEGACY_LAYOUT_PATTERN = re.compile(r"instance\.yml|conf/targets|agent/config\.yml")

LEGACY_LAYOUT_ALLOWLIST: dict[str, str] = {
    "agent_foundry/_legacy_layout.py": "defines reject_legacy_layout, which detects the leftover files",
}

DEPRECATION_CATEGORIES = frozenset({"DeprecationWarning", "PendingDeprecationWarning", "FutureWarning"})

DEPRECATION_ALLOWLIST: dict[str, str] = {
    "agent_foundry/bricks.py": "_StandardLayoutBrick alias for forks (#721); remove one release after the rename",
}


def _production_files(suffixes: frozenset[str]) -> list[Path]:
    files: list[Path] = []
    for root in SCAN_ROOTS:
        for path in root.rglob("*"):
            rel = path.relative_to(REPO_ROOT)
            if path.suffix in suffixes and path.is_file() and EXCLUDED_DIRS.isdisjoint(rel.parts):
                files.append(path)
    return sorted(files)


def _rel(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def legacy_layout_lines(text: str) -> list[int]:
    """1-based line numbers in *text* that name a legacy layout file."""
    return [n for n, line in enumerate(text.splitlines(), start=1) if LEGACY_LAYOUT_PATTERN.search(line)]


def _is_deprecation_category(node: ast.AST | None) -> bool:
    if isinstance(node, ast.Name):
        return node.id in DEPRECATION_CATEGORIES
    if isinstance(node, ast.Attribute):
        return node.attr in DEPRECATION_CATEGORIES
    return False


def _is_warn_call(node: ast.Call) -> bool:
    func = node.func
    return (isinstance(func, ast.Name) and func.id == "warn") or (
        isinstance(func, ast.Attribute) and func.attr == "warn"
    )


def _emits_deprecation(node: ast.Call) -> bool:
    """``warn(msg, Deprecation…)``, ``warn(…, category=Deprecation…)`` or ``Deprecation…(...)``."""
    if _is_deprecation_category(node.func):
        return True
    if not _is_warn_call(node):
        return False
    category = node.args[1] if len(node.args) > 1 else None
    category = next((kw.value for kw in node.keywords if kw.arg == "category"), category)
    return _is_deprecation_category(category)


def _docstring_ids(tree: ast.AST) -> set[int]:
    owners = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    return {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, owners)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }


def deprecation_emissions(tree: ast.AST) -> list[int]:
    """Line numbers in *tree* that emit or echo a deprecation warning (docstrings excluded)."""
    docstrings = _docstring_ids(tree)
    lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _emits_deprecation(node):
            lines.append(node.lineno)
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
            and any(category in node.value for category in DEPRECATION_CATEGORIES)
        ):
            lines.append(node.lineno)
    return sorted(lines)


def _emissions_in(path: Path) -> list[int]:
    return deprecation_emissions(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))


def test_production_files_discovered() -> None:
    assert _production_files(TEXT_SUFFIXES), f"No files discovered under {[str(r) for r in SCAN_ROOTS]}."


@pytest.mark.parametrize("path", _production_files(TEXT_SUFFIXES), ids=_rel)
def test_file_does_not_name_legacy_layout(path: Path) -> None:
    if _rel(path) in LEGACY_LAYOUT_ALLOWLIST:
        return
    offenders = legacy_layout_lines(path.read_text(encoding="utf-8"))
    assert not offenders, (
        f"{_rel(path)} names the removed instance.yml / conf/targets / agent/config.yml layout; config "
        "lives in foundry.toml and payloads/<name>/payload.yml. Leftover detection belongs in "
        "agent_foundry/_legacy_layout.py:\n" + "\n".join(f"  L{n}" for n in offenders)
    )


@pytest.mark.parametrize("module", _production_files(frozenset({".py"})), ids=_rel)
def test_module_does_not_emit_deprecation(module: Path) -> None:
    if _rel(module) in DEPRECATION_ALLOWLIST:
        return
    offenders = _emissions_in(module)
    assert not offenders, (
        f"{_rel(module)} emits a deprecation warning. Remove the old path instead, or, for a "
        "deliberate public deprecation window, add the module to DEPRECATION_ALLOWLIST with a reason:\n"
        + "\n".join(f"  L{n}" for n in offenders)
    )


def test_allowlists_have_no_stale_entries() -> None:
    stale_layout = [
        rel
        for rel in LEGACY_LAYOUT_ALLOWLIST
        if not (REPO_ROOT / rel).is_file() or not legacy_layout_lines((REPO_ROOT / rel).read_text(encoding="utf-8"))
    ]
    stale_deprecation = [
        rel for rel in DEPRECATION_ALLOWLIST if not (REPO_ROOT / rel).is_file() or not _emissions_in(REPO_ROOT / rel)
    ]
    assert not stale_layout, f"LEGACY_LAYOUT_ALLOWLIST entries no longer match; remove them: {stale_layout}"
    assert not stale_deprecation, f"DEPRECATION_ALLOWLIST entries no longer emit; remove them: {stale_deprecation}"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("name = 'x'\n", []),
        ("see instances/<name>/instance.yml\n", [1]),
        ("a\nconf/targets/dev.yml\n", [2]),
        ("set mcp_only in agent/config.yml\n", [1]),
        ("foundry.toml\n", []),
        ("payloads/agent/payload.yml\nwarehouse/config.yml\n", []),
    ],
)
def test_legacy_layout_detector(text: str, expected: list[int]) -> None:
    assert legacy_layout_lines(text) == expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("warnings.warn('x', DeprecationWarning)", [1]),
        ("warn('x', category=FutureWarning)", [1]),
        ("warnings.warn('x', builtins.PendingDeprecationWarning, stacklevel=2)", [1]),
        ("raise DeprecationWarning('x')", [1]),
        ("typer.echo('DeprecationWarning: use model:')", [1]),
        ("warnings.warn('x', UserWarning)", []),
        ("warnings.filterwarnings('ignore', category=DeprecationWarning)", []),
        ("def f():\n    '''Never emits DeprecationWarning.'''", []),
    ],
)
def test_deprecation_detector(source: str, expected: list[int]) -> None:
    assert deprecation_emissions(ast.parse(source)) == expected
