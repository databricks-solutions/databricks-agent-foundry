"""Where tests live: each owner's ``tests/unit/`` (offline) or ``tests/live/`` (deployment).

The ``live`` marker follows the folder (``agent_foundry.pytest_plugin``),
so an explicit ``pytest.mark.live`` elsewhere would put a workspace test
into the offline run's blind spot, and a unit test that asks for a live
fixture would silently skip offline.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

import pytest

from agent_foundry.testing.live import is_live_path

REPO = Path(__file__).resolve().parents[2]
_OWNER_ROOTS = ("agent_foundry", "bricks", "payloads", "examples")
_SOURCE_ROOTS = (*_OWNER_ROOTS, "tests")
_LIVE_FIXTURES = frozenset({"live_target", "live_ws", "live_variables", "gone_variables"})
# Both mean "needs a deployment", so both belong in tests/live/.
_DEPLOYMENT_MARKS = frozenset({"live", "gone"})
_SKIP_DIRS = frozenset({".venv", "node_modules", "__pycache__", "_scaffold"})


def _python_files(root: Path) -> Iterator[Path]:
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(REPO)
        if _SKIP_DIRS.isdisjoint(rel.parts):
            yield path


def _source_files() -> Iterator[Path]:
    for root in _SOURCE_ROOTS:
        yield from _python_files(REPO / root)


def _test_modules(root: Path) -> Iterator[Path]:
    for path in _python_files(root):
        if path.name.startswith("test_") or path.name == "conftest.py":
            yield path


def _is_unit_path(path: Path) -> bool:
    parts = path.relative_to(REPO).parts
    return any(pair == ("tests", "unit") for pair in zip(parts, parts[1:]))


def _is_deployment_mark(node: ast.AST) -> bool:
    """``pytest.mark.live`` / ``pytest.mark.gone`` as attribute chains (strings do not count)."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr in _DEPLOYMENT_MARKS
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "mark"
    )


def _requested_fixtures(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.update(arg.arg for arg in node.args.args + node.args.kwonlyargs)
    return names


def _parse(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


@pytest.mark.parametrize("owner", _OWNER_ROOTS)
def test_owner_tests_sit_under_unit_or_live(owner: str) -> None:
    misplaced = [
        str(path.relative_to(REPO))
        for path in _test_modules(REPO / owner)
        if not (_is_unit_path(path) or is_live_path(path))
    ]
    assert misplaced == [], f"move these under tests/unit/ or tests/live/: {misplaced}"


def test_no_live_marker_outside_live_folders() -> None:
    offenders = [
        str(path.relative_to(REPO))
        for path in _source_files()
        if not is_live_path(path) and any(map(_is_deployment_mark, ast.walk(_parse(path))))
    ]
    assert offenders == [], f"live / gone tests belong in a tests/live/ folder (the marker follows it): {offenders}"


def test_unit_tests_request_no_live_fixture() -> None:
    offenders = {
        str(path.relative_to(REPO)): sorted(found)
        for path in _source_files()
        if _is_unit_path(path) and (found := _requested_fixtures(_parse(path)) & _LIVE_FIXTURES)
    }
    assert offenders == {}, f"tests/unit must stay offline; live fixtures requested: {offenders}"
