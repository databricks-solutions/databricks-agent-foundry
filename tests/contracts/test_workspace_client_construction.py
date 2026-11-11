"""WorkspaceClient construction tripwire (issue #582).

Production code must obtain a ``databricks.sdk.WorkspaceClient`` from a
sanctioned factory rather than calling ``WorkspaceClient(...)`` itself:

* ``agent_foundry.workspace_client`` -- framework, bricks, payload deploy
  code, and payload runtimes that ship with the agent-foundry wheel.
  Honours the target profile / host and stamps the product User-Agent.
* ``payloads/agent/runtime/request_context.py`` -- the agent App runtime's service
  principal and OBO clients.

The scan is AST-based and repo-wide, so a direct construction anywhere
(including lazy, function-local ones) fails here.  Calls inside
``if TYPE_CHECKING:`` blocks are ignored.  Exemptions live in
:data:`CONSTRUCTOR_ALLOWLIST` (exact files) and
:data:`CONSTRUCTOR_EXEMPT_GLOBS` (code that runs where agent-foundry is
not installed); each carries a reason.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

SCAN_ROOTS: tuple[Path, ...] = (
    REPO_ROOT / "agent_foundry",
    REPO_ROOT / "bricks",
    REPO_ROOT / "payloads",
    REPO_ROOT / "examples",
)

EXCLUDED_DIRS = frozenset({"tests", "docs", "__pycache__"})

CONSTRUCTOR_ALLOWLIST: dict[str, str] = {
    "agent_foundry/workspace_client.py": "the shared WorkspaceClient factory",
    "payloads/agent/runtime/request_context.py": "agent App runtime factory (service principal + OBO clients)",
}

CONSTRUCTOR_EXEMPT_GLOBS: dict[str, str] = {
    "examples/*/jobs/*/notebooks/*.py": (
        "job notebooks run on Databricks job compute without agent-foundry installed; "
        "WorkspaceClient() there uses the job's own credentials"
    ),
}


def _is_exempt(rel: Path) -> bool:
    return rel.as_posix() in CONSTRUCTOR_ALLOWLIST or any(rel.match(g) for g in CONSTRUCTOR_EXEMPT_GLOBS)


def _iter_production_modules() -> list[Path]:
    modules: list[Path] = []
    for root in SCAN_ROOTS:
        for py in root.rglob("*.py"):
            if EXCLUDED_DIRS.isdisjoint(py.relative_to(REPO_ROOT).parts):
                modules.append(py)
    return sorted(modules)


def _is_type_checking_test(test: ast.AST) -> bool:
    return isinstance(test, ast.Name) and test.id == "TYPE_CHECKING"


def _is_workspace_client_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return (isinstance(func, ast.Name) and func.id == "WorkspaceClient") or (
        isinstance(func, ast.Attribute) and func.attr == "WorkspaceClient"
    )


def workspace_client_calls(tree: ast.AST) -> list[ast.Call]:
    """``WorkspaceClient(...)`` calls in *tree*, excluding ``if TYPE_CHECKING:`` blocks."""
    type_checking_ids = {
        id(child)
        for node in ast.walk(tree)
        if isinstance(node, ast.If) and _is_type_checking_test(node.test)
        for child in ast.walk(node)
    }
    return [node for node in ast.walk(tree) if id(node) not in type_checking_ids and _is_workspace_client_call(node)]


def _calls_in(path: Path) -> list[ast.Call]:
    return workspace_client_calls(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))


def test_production_modules_discovered() -> None:
    assert _iter_production_modules(), f"No modules discovered under {[str(r) for r in SCAN_ROOTS]}."


@pytest.mark.parametrize(
    "module",
    _iter_production_modules(),
    ids=lambda p: p.relative_to(REPO_ROOT).as_posix(),
)
def test_module_does_not_construct_workspace_client(module: Path) -> None:
    rel = module.relative_to(REPO_ROOT)
    if _is_exempt(rel):
        return
    offenders = _calls_in(module)
    assert not offenders, (
        f"{rel.as_posix()} constructs WorkspaceClient() directly. Use "
        "agent_foundry.workspace_client.get_workspace_client / prefer_workspace_client "
        "(or the agent runtime factory in payloads/agent/runtime/request_context.py). If this is a "
        "deliberate exception, add it to CONSTRUCTOR_ALLOWLIST with a reason:\n"
        + "\n".join(f"  L{call.lineno}" for call in offenders)
    )


def test_allowlist_has_no_stale_entries() -> None:
    stale = [rel for rel in CONSTRUCTOR_ALLOWLIST if not (REPO_ROOT / rel).exists() or not _calls_in(REPO_ROOT / rel)]
    assert not stale, f"CONSTRUCTOR_ALLOWLIST entries no longer construct WorkspaceClient; remove them: {stale}"


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("WorkspaceClient()", 1),
        ("sdk.WorkspaceClient(host='h')", 1),
        ("def f():\n    from databricks.sdk import WorkspaceClient\n    return WorkspaceClient()", 1),
        ("if TYPE_CHECKING:\n    ws = WorkspaceClient()", 0),
        ("FakeWorkspaceClient()", 0),
        ("ws: WorkspaceClient = get_workspace_client()", 0),
    ],
)
def test_detector(source: str, expected: int) -> None:
    assert len(workspace_client_calls(ast.parse(source))) == expected
