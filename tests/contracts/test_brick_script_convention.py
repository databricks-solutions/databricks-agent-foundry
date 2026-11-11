"""
Brick-script convention test (issue #149).

Every concrete (non-stub) ``bricks/<brick>/scripts/<action>.py`` module
must expose a ``main(argv: list[str] | None = None) -> int`` entrypoint.
This is the contract the CLI dispatcher
(:mod:`agent_foundry.deploy._brick_registry`) relies on when forwarding
``agent-foundry brick <name> <action>`` argv to the script.

The convention was established by issue #96 (CLI consolidation, 2026-05-04)
and is documented in CONTRIBUTING.md.  This test pins it so a future
brick contributor cannot silently break dispatch by skipping the
``main(argv)`` shape.

Stub modules (TODO files with no implementation) are excluded by an
explicit allow-list.  When a stub is implemented it must be removed
from :data:`STUB_MODULES` at the same time.
"""

from __future__ import annotations

import ast
import importlib
import inspect
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BRICKS_DIR = REPO_ROOT / "bricks"

# Modules that are intentionally stubs (TODO placeholders) and
# therefore exempt from the ``main(argv)`` requirement.  Listed with
# their dotted import paths.  When a stub is implemented, drop its
# entry here in the same PR.
STUB_MODULES: frozenset[str] = frozenset()

# Modules under ``bricks/<brick>/scripts/`` that are *internal
# helpers* invoked from another brick's deploy code path, not CLI
# entrypoints.  The CLI dispatcher in
# :mod:`agent_foundry.deploy._brick_registry` therefore never
# resolves these dotted paths, and a ``main(argv)`` wrapper would be
# dead code.  Add a one-line reason below; reviewers should push
# back on any addition that *should* be CLI-reachable.
# Currently empty: the Lakebase provisioner that used to live at
# ``bricks/app/scripts/lakebase.py`` moved into the agent payload
# (``payloads/agent/deploy/lakebase.py``) in the #338 cutover, so there
# are no non-CLI helper scripts left under ``bricks/*/scripts/``.
NON_CLI_HELPER_MODULES: frozenset[str] = frozenset()


def _all_brick_script_modules() -> list[str]:
    """Discover every ``bricks/<brick>/scripts/<action>.py`` and return
    its dotted import path.  Excludes ``__init__.py`` (no main expected),
    the documented stubs in :data:`STUB_MODULES`, and the documented
    internal helpers in :data:`NON_CLI_HELPER_MODULES`.
    """
    modules: list[str] = []
    for script_path in sorted(BRICKS_DIR.glob("*/scripts/*.py")):
        if script_path.name == "__init__.py":
            continue
        rel = script_path.relative_to(REPO_ROOT).with_suffix("")
        dotted = ".".join(rel.parts)
        if dotted in STUB_MODULES:
            continue
        if dotted in NON_CLI_HELPER_MODULES:
            continue
        modules.append(dotted)
    return modules


@pytest.mark.parametrize("dotted", _all_brick_script_modules())
def test_brick_script_exposes_main_argv(dotted: str) -> None:
    """Every concrete brick script must expose a ``main(argv) -> int``.

    The CLI dispatcher in :mod:`agent_foundry.deploy._brick_registry`
    forwards ``argv`` to this function unchanged, so the signature is
    a hard contract, not a convention.
    """
    module = importlib.import_module(dotted)
    main = getattr(module, "main", None)
    assert main is not None, (
        f"{dotted} has no `main` attribute; expected `def main(argv: list[str] | None = None) -> int`."
    )
    assert callable(main), f"{dotted}.main is not callable"

    sig = inspect.signature(main)
    params = list(sig.parameters.values())
    assert len(params) == 1, f"{dotted}.main must accept exactly one argument (argv); got signature {sig}"
    argv_param = params[0]
    assert argv_param.name == "argv", f"{dotted}.main's first arg must be named 'argv'; got {argv_param.name!r}"
    assert argv_param.default is None, (
        f"{dotted}.main's argv must default to None (so the function "
        f"can be called from the dispatcher); got default={argv_param.default!r}"
    )


def test_no_undocumented_stubs_left_behind() -> None:
    """Every entry in :data:`STUB_MODULES` must still be a stub.

    Catches the inverse drift: a stub that was implemented but
    forgotten in the allow-list (which would silently exclude it from
    the convention check).  We treat any module that defines ``main``
    as no-longer-a-stub.
    """
    still_stubs: list[str] = []
    for dotted in STUB_MODULES:
        module = importlib.import_module(dotted)
        if getattr(module, "main", None) is None:
            still_stubs.append(dotted)
    extras = sorted(STUB_MODULES - set(still_stubs))
    assert not extras, f"These modules now define a `main` and should be removed from STUB_MODULES: {extras}"


def test_non_cli_helpers_do_not_define_main() -> None:
    """Every entry in :data:`NON_CLI_HELPER_MODULES` must remain a
    helper -- if a future change adds a ``main(argv)`` to one of
    these, it is now CLI-reachable and the entry should be dropped
    from the allow-list (otherwise it silently bypasses the
    signature-shape check above).
    """
    leaked: list[str] = []
    for dotted in NON_CLI_HELPER_MODULES:
        module = importlib.import_module(dotted)
        if getattr(module, "main", None) is not None:
            leaked.append(dotted)
    assert not leaked, (
        f"These helper modules now define a `main` and should be removed from NON_CLI_HELPER_MODULES: {leaked}"
    )


_PAYLOAD_DEPLOY_ROOTS: tuple[Path, ...] = (
    REPO_ROOT / "payloads" / "agent" / "deploy",
    REPO_ROOT / "payloads" / "rest_mcp" / "deploy",
)


def _deploy_script_paths() -> list[Path]:
    """Brick scripts plus payload deploy modules (not their tests)."""
    paths: list[Path] = []
    for script_path in sorted(BRICKS_DIR.glob("*/scripts/*.py")):
        if script_path.name == "__init__.py":
            continue
        paths.append(script_path)
    for root in _PAYLOAD_DEPLOY_ROOTS:
        for script_path in sorted(root.rglob("*.py")):
            if "tests" in script_path.parts:
                continue
            paths.append(script_path)
    return paths


def _script_imports_or_calls_get_workspace_client(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "") == "agent_foundry.workspace_client":
            if any(alias.name == "get_workspace_client" for alias in node.names):
                return True
        if isinstance(node, ast.Name) and node.id == "get_workspace_client":
            return True
        if isinstance(node, ast.Attribute) and node.attr == "get_workspace_client":
            return True
    return False


def test_brick_scripts_do_not_import_get_workspace_client() -> None:
    """Scripts take an injected client; the factory is only via prefer_workspace_client."""
    offenders: list[str] = []
    for script_path in _deploy_script_paths():
        if _script_imports_or_calls_get_workspace_client(script_path):
            offenders.append(str(script_path.relative_to(REPO_ROOT)))
    assert not offenders, (
        "These scripts import or call get_workspace_client; pass ws= and use "
        f"prefer_workspace_client instead: {offenders}"
    )
