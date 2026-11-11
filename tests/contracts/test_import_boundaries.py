"""Import-boundary tripwires.

This module pins two complementary rules:

1. **Runtime-never-imports-deploy** (issue #83, 68-D).
   The agent container that Databricks Apps runs executes a narrow
   slice of the repo at request time: the ``agent`` payload runtime
   (``payloads/agent/runtime/``, incl. ``agent_runner.py`` and the
   MCP/A2A endpoint adapters) plus the small handful of runtime
   modules under ``bricks/app/``.
   Deploy code (every ``bricks/**/scripts/``, the orchestrator, the
   assembler) MUST NOT be importable from that runtime slice, for
   two reasons:

   a. Deploy modules pull in ``databricks-sdk`` + heavy YAML parsing
      that the request path doesn't need; reducing runtime RAM
      matters on Databricks Apps.
   b. A future split into a separate ``agent-foundry-runtime`` wheel
      (a #68 follow-up) requires the two layers to be independent
      *today*, not later when a regression has already sneaked in.

2. **Non-runtime-never-top-level-imports-runtime-extra** (issue #154).
   Packages that exist only in the ``[runtime]`` extra (``agents``,
   ``slowapi``, ...) MUST NOT be imported at module top
   level outside the runtime slice.  Deploy / eval / call paths run
   on machines that don't necessarily have the ``[runtime]`` extra
   installed; a top-level import would raise ``ModuleNotFoundError``
   at import time and break the verb (e.g. ``agent-foundry deploy``)
   even when no runtime feature is being used.

Both checks are AST-based -- they walk import statements without
actually executing them, so the test runs cleanly even when the
target packages aren't installed.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# What counts as runtime vs deploy?
# ---------------------------------------------------------------------------

# Everything under these directories counts as runtime code, except
# files matching the exclude patterns below.  The agent's request-path
# runtime lives in the ``agent`` payload (``payloads/agent/runtime``,
# issue #338); the ``rest_mcp`` proxy's request-path runtime is its
# in-container MCP server (``payloads/rest_mcp/runtime``, issue #326) --
# it legitimately imports ``mcp`` / ``starlette`` / ``uvicorn`` at module
# top level.  The ``bricks/agent`` and ``bricks/agent_runtime`` compat
# shims were removed in agent-foundry 2.0.
RUNTIME_ROOTS = [
    REPO_ROOT / "payloads" / "agent" / "runtime",
    REPO_ROOT / "payloads" / "rest_mcp" / "runtime",
    REPO_ROOT / "bricks" / "app",
]

# These subdirectories under a runtime root are *not* runtime.
NON_RUNTIME_SUBDIRS = {"scripts", "tests", "docs", "__pycache__"}

# Matched against the dotted module path.  Top-level ``brick.py``
# adapters are deploy-time only (they're what the orchestrator calls)
# so exclude them from the runtime sweep.
DEPLOY_ONLY_MODULES = {
    "bricks.app.brick",
    "bricks.app.scripts.start_app",
}

# An import target matching any of these regexes is forbidden in
# runtime code.
FORBIDDEN_TARGETS: list[re.Pattern[str]] = [
    re.compile(r"^bricks\.[^.]+\.scripts(\..+)?$"),  # bricks/<any>/scripts/
    re.compile(r"^agent_foundry\.deploy(\..+)?$"),  # agent_foundry/deploy/
    # payload deploy-time code (e.g. the relocated skills assembler /
    # MCP step, issue #338).  The request-path runtime reads the
    # assembled artefacts / workspace state, never the deploy modules.
    re.compile(r"^payloads\.agent\.deploy(\..+)?$"),
]


def _is_runtime_file(path: Path) -> bool:
    parts = path.relative_to(REPO_ROOT).parts
    if any(seg in NON_RUNTIME_SUBDIRS for seg in parts):
        return False
    module_path = ".".join(parts).removesuffix(".py")
    return module_path not in DEPLOY_ONLY_MODULES


def _iter_runtime_modules() -> list[Path]:
    modules: list[Path] = []
    for root in RUNTIME_ROOTS:
        if not root.is_dir():
            continue
        for py in root.rglob("*.py"):
            if _is_runtime_file(py):
                modules.append(py)
    return sorted(modules)


def _forbidden_imports(path: Path) -> list[tuple[int, str]]:
    """Return (lineno, module) for every forbidden import in *path*."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    offenders: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            target = node.module
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if _matches_forbidden(alias.name):
                    offenders.append((node.lineno, alias.name))
            continue
        else:
            continue
        if _matches_forbidden(target):
            offenders.append((node.lineno, target))
    return offenders


def _matches_forbidden(module: str) -> bool:
    return any(p.match(module) for p in FORBIDDEN_TARGETS)


def test_runtime_roots_discovered() -> None:
    modules = _iter_runtime_modules()
    assert modules, f"No runtime modules discovered under {[str(r) for r in RUNTIME_ROOTS]}. Check test fixtures."


@pytest.mark.parametrize("runtime_module", _iter_runtime_modules(), ids=str)
def test_runtime_module_does_not_import_deploy(runtime_module: Path) -> None:
    """Each runtime module's AST contains no imports of deploy code."""
    offenders = _forbidden_imports(runtime_module)
    assert not offenders, (
        f"Runtime module {runtime_module.relative_to(REPO_ROOT)} imports "
        "deploy-only code. 68-D (#83) forbids this so the agent container "
        "can ship without the deploy machinery:\n" + "\n".join(f"  L{line}: {target}" for line, target in offenders)
    )


# ---------------------------------------------------------------------------
# Issue #154: non-runtime modules MUST NOT top-level-import runtime-extra deps
# ---------------------------------------------------------------------------
#
# Top-level packages that are pulled in *only* by the ``[runtime]`` extra
# in pyproject.toml (i.e. not also in ``[eval]``, ``[call]``, ``[dev]``,
# or the base dep set).  A top-level ``import agents`` from a deploy or
# eval module would raise ``ModuleNotFoundError`` when invoked from a
# machine that didn't install ``[runtime]`` -- breaking ``agent-foundry
# deploy``, the eval harness, etc.
#
# Notes on omissions:
# - ``mlflow`` is in both ``[runtime]`` and ``[eval]``, so eval scorers
#   may legitimately import it at top level.
# - ``a2a`` is in both ``[runtime]`` and ``[call]``, so the local-caller
#   (``agent-foundry call a2a``) may import it at top level.
# - ``protobuf`` is a transitive dep of multiple ecosystems; not on
#   this list because non-runtime top-level imports are not the bug.

RUNTIME_ONLY_TOP_LEVEL_PACKAGES: frozenset[str] = frozenset(
    {
        "agents",  # openai-agents
        "databricks_openai",  # databricks-openai
        "databricks_ai_bridge",  # databricks-ai-bridge
        "slowapi",  # slowapi
        "mcp",  # mcp (server-side runtime adapter)
        "websockets",  # websockets (chat WebSocket relay)
    }
)


# Roots to scan for the inverse rule.  Everything under these directories
# is considered "non-runtime" by default; the runtime slice is then
# carved out with :func:`_is_runtime_file` and the test files / docs are
# ignored unconditionally.
NON_RUNTIME_SCAN_ROOTS = [
    REPO_ROOT / "agent_foundry",
    REPO_ROOT / "bricks",
    REPO_ROOT / "payloads",
]

# Paths under :data:`NON_RUNTIME_SCAN_ROOTS` that are *not* checked.
# Test files import runtime symbols freely (they may use ``patch`` or
# ``MagicMock`` against a mocked-out runtime); the rule is about
# production import time, not test time.
NON_RUNTIME_EXCLUDE_DIRS = {"tests", "docs", "__pycache__"}


def _iter_non_runtime_modules() -> list[Path]:
    """Every ``.py`` file outside the runtime slice and outside the
    test/docs subtrees.  Used to enforce that the runtime-extra
    packages aren't pulled in at deploy / eval / call import time.
    """
    runtime_paths = {p.resolve() for p in _iter_runtime_modules()}
    modules: list[Path] = []
    for root in NON_RUNTIME_SCAN_ROOTS:
        if not root.is_dir():
            continue
        for py in root.rglob("*.py"):
            if any(seg in NON_RUNTIME_EXCLUDE_DIRS for seg in py.relative_to(REPO_ROOT).parts):
                continue
            if py.resolve() in runtime_paths:
                continue
            modules.append(py)
    return sorted(modules)


def _top_level_runtime_extra_imports(path: Path) -> list[tuple[int, str]]:
    """Return ``(lineno, module)`` for every top-level import in *path*
    whose root package is in :data:`RUNTIME_ONLY_TOP_LEVEL_PACKAGES`.

    Only inspects statements at module scope (``tree.body``).  Imports
    nested inside functions / classes are deliberately fine -- that's
    the lazy pattern the rule is meant to encourage.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    offenders: list[tuple[int, str]] = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            root = node.module.split(".", 1)[0]
            if root in RUNTIME_ONLY_TOP_LEVEL_PACKAGES:
                offenders.append((node.lineno, node.module))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                if root in RUNTIME_ONLY_TOP_LEVEL_PACKAGES:
                    offenders.append((node.lineno, alias.name))
    return offenders


def test_non_runtime_modules_discovered() -> None:
    modules = _iter_non_runtime_modules()
    assert modules, (
        f"No non-runtime modules discovered under {[str(r) for r in NON_RUNTIME_SCAN_ROOTS]}. Check test fixtures."
    )


@pytest.mark.parametrize("non_runtime_module", _iter_non_runtime_modules(), ids=str)
def test_non_runtime_module_does_not_top_level_import_runtime_extra(
    non_runtime_module: Path,
) -> None:
    """Issue #154: deploy / eval / call modules must keep ``[runtime]``-only
    deps out of their top-level imports so they remain importable on
    machines that didn't install the ``[runtime]`` extra.  Move the
    offending import into the function or class that uses it.
    """
    offenders = _top_level_runtime_extra_imports(non_runtime_module)
    assert not offenders, (
        f"Non-runtime module {non_runtime_module.relative_to(REPO_ROOT)} "
        "top-level-imports a [runtime]-extra package.  Move the import "
        "inside the function / class that uses it (lazy pattern) or "
        "relocate the module into the runtime slice "
        f"({[str(r.relative_to(REPO_ROOT)) for r in RUNTIME_ROOTS]}):\n"
        + "\n".join(f"  L{line}: {target}" for line, target in offenders)
    )


# ---------------------------------------------------------------------------
# Issue #338: framework + bricks MUST NOT import a specific payload
# ---------------------------------------------------------------------------
#
# The framework (``agent_foundry/``) and the generic bricks (``bricks/``)
# are payload-agnostic.  They reach a payload only through the structural
# ``AppPayloadAdapter`` seam plus the neutral ``payload.yml`` manifest --
# never by importing ``payloads.<name>`` internals (its config, deploy
# glue, runtime, chat-history / Lakebase state, ...).  This keeps the
# framework stable as payloads evolve and guarantees a second,
# non-agent payload cannot break the App host.
#
# This tripwire walks the AST (catching lazy, function-local imports too,
# which is how the Lakebase coupling repeatedly crept back into
# ``bricks/app/brick.py``), so the *class* of regression is blocked, not
# just one instance.  Exactly the handful of deliberate, reviewer-
# justified bridges below are exempt -- keep the list as small as
# possible; a new entry means a new framework->payload coupling.

PAYLOAD_IMPORT_ALLOWLIST: dict[str, str] = {
    # The single architectural bridge: the payload-adapter registry,
    # which lazily instantiates the built-in adapter (mirrors
    # ``agent_foundry.bricks._builtin_bricks``).
    "agent_foundry/deploy/payload_adapters.py": (
        "payload-adapter registry -- the one sanctioned framework->payload bridge"
    ),
    # Eval imports the agent payload's runtime module to trigger its
    # ``@invoke()`` registration before scoring it.
    "bricks/evaluation/scripts/run_evaluation.py": (
        "eval loads the agent runtime module to trigger @invoke() registration"
    ),
}

# Production roots that must stay payload-agnostic.
PAYLOAD_AGNOSTIC_ROOTS = [
    REPO_ROOT / "agent_foundry",
    REPO_ROOT / "bricks",
]


def _iter_production_modules(roots: list[Path]) -> list[Path]:
    """Every production ``.py`` under *roots* (test / docs subtrees excluded)."""
    modules: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        for py in root.rglob("*.py"):
            parts = py.relative_to(REPO_ROOT).parts
            if any(seg in NON_RUNTIME_EXCLUDE_DIRS for seg in parts):
                continue
            modules.append(py)
    return sorted(modules)


def _package_imports(path: Path, package: str) -> list[tuple[int, str]]:
    """Return ``(lineno, module)`` for every ``<package>(.*)`` import in
    *path* -- top-level OR nested (lazy imports count)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    offenders: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            if node.module == package or node.module.startswith(f"{package}."):
                offenders.append((node.lineno, node.module))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == package or alias.name.startswith(f"{package}."):
                    offenders.append((node.lineno, alias.name))
    return offenders


def test_payload_agnostic_roots_discovered() -> None:
    modules = _iter_production_modules(PAYLOAD_AGNOSTIC_ROOTS)
    assert modules, f"No modules discovered under {[str(r) for r in PAYLOAD_AGNOSTIC_ROOTS]}. Check test fixtures."


@pytest.mark.parametrize("module", _iter_production_modules(PAYLOAD_AGNOSTIC_ROOTS), ids=str)
def test_framework_module_does_not_import_payload(module: Path) -> None:
    """The framework + generic bricks must not import a specific payload
    (issue #338).  Talk to payloads only via the ``AppPayloadAdapter``
    seam + the neutral ``payload.yml``; the only exemptions are the
    reviewer-justified bridges in :data:`PAYLOAD_IMPORT_ALLOWLIST`.
    """
    rel = module.relative_to(REPO_ROOT).as_posix()
    if rel in PAYLOAD_IMPORT_ALLOWLIST:
        return
    offenders = _package_imports(module, "payloads")
    assert not offenders, (
        f"Payload-agnostic module {rel} imports a specific payload "
        "(``payloads.*``).  The framework/bricks must reach payloads only "
        "through the AppPayloadAdapter seam + payload.yml, not by importing "
        "their internals (issue #338).  If this is a deliberate bridge, add "
        "it to PAYLOAD_IMPORT_ALLOWLIST with a justification:\n"
        + "\n".join(f"  L{line}: {target}" for line, target in offenders)
    )


def test_payload_import_allowlist_has_no_stale_entries() -> None:
    """Every allow-list entry must still exist and still import a payload.

    Stops the list from accreting dead exemptions: when a bridge is
    removed or relocated (e.g. the ``mcp inspect`` CLI moves into the
    payload), its entry must be deleted in the same change.
    """
    stale: list[str] = []
    for rel in PAYLOAD_IMPORT_ALLOWLIST:
        path = REPO_ROOT / rel
        if not path.exists() or not _package_imports(path, "payloads"):
            stale.append(rel)
    assert not stale, f"PAYLOAD_IMPORT_ALLOWLIST entries no longer import a payload; remove them: {stale}"


# ---------------------------------------------------------------------------
# Issue #715: payloads MUST NOT import brick code
# ---------------------------------------------------------------------------
#
# A payload states which bricks it needs by *name* in ``payload.requires``
# (``PayloadBrickRequirement``); the orchestrator enforces that through
# ``agent_foundry.payload_requirements`` -- no import between the two.
# Payload code importing ``bricks.*`` is a separate, code-level coupling,
# so it is blocked here except for the reviewed entries below.  An entry
# is not a requirement: the brick may be optional for the payload.

BRICK_IMPORT_ALLOWLIST: dict[str, str] = {
    "payloads/agent/deploy/gateway_reach.py": (
        "deploy-time reachability warning against the optional ai_gateway brick's public config"
    ),
}

BRICK_AGNOSTIC_ROOTS = [REPO_ROOT / "payloads"]


def test_brick_agnostic_roots_discovered() -> None:
    modules = _iter_production_modules(BRICK_AGNOSTIC_ROOTS)
    assert modules, f"No modules discovered under {[str(r) for r in BRICK_AGNOSTIC_ROOTS]}. Check test fixtures."


@pytest.mark.parametrize("module", _iter_production_modules(BRICK_AGNOSTIC_ROOTS), ids=str)
def test_payload_module_does_not_import_brick(module: Path) -> None:
    """Payload code must not import brick code (issue #715); declare
    needed bricks in ``payload.requires`` instead.  The only exemptions
    are the reviewed entries in :data:`BRICK_IMPORT_ALLOWLIST`.
    """
    rel = module.relative_to(REPO_ROOT).as_posix()
    if rel in BRICK_IMPORT_ALLOWLIST:
        return
    offenders = _package_imports(module, "bricks")
    assert not offenders, (
        f"Payload module {rel} imports brick code (``bricks.*``).  Declare the "
        "brick in payload.requires if the payload needs it deployed; if the "
        "import is a deliberate bridge, add it to BRICK_IMPORT_ALLOWLIST with "
        "a justification (issue #715):\n" + "\n".join(f"  L{line}: {target}" for line, target in offenders)
    )


def test_brick_import_allowlist_has_no_stale_entries() -> None:
    """Every allow-list entry must still exist and still import a brick."""
    stale = [
        rel
        for rel in BRICK_IMPORT_ALLOWLIST
        if not (REPO_ROOT / rel).exists() or not _package_imports(REPO_ROOT / rel, "bricks")
    ]
    assert not stale, f"BRICK_IMPORT_ALLOWLIST entries no longer import a brick; remove them: {stale}"


# ---------------------------------------------------------------------------
# Issue #714: the framework MUST NOT import brick code
# ---------------------------------------------------------------------------
#
# The framework reaches brick scripts through the CLI action registry
# (``agent_foundry.deploy._brick_registry.resolve_action``) and brick
# adapters through the built-in adapter registry below.  Any other
# ``bricks.*`` import couples the framework to one plugin.

FRAMEWORK_BRICK_IMPORT_ALLOWLIST: dict[str, str] = {
    "agent_foundry/bricks.py": "built-in brick adapter registry -- the one sanctioned framework->brick bridge",
}

FRAMEWORK_ROOTS = [REPO_ROOT / "agent_foundry"]


def test_framework_roots_discovered() -> None:
    modules = _iter_production_modules(FRAMEWORK_ROOTS)
    assert modules, f"No modules discovered under {[str(r) for r in FRAMEWORK_ROOTS]}. Check test fixtures."


@pytest.mark.parametrize("module", _iter_production_modules(FRAMEWORK_ROOTS), ids=str)
def test_framework_module_does_not_import_brick(module: Path) -> None:
    """Framework code must not import brick code (issue #714); resolve
    brick scripts via ``resolve_action``.  The only exemptions are the
    reviewed entries in :data:`FRAMEWORK_BRICK_IMPORT_ALLOWLIST`.
    """
    rel = module.relative_to(REPO_ROOT).as_posix()
    if rel in FRAMEWORK_BRICK_IMPORT_ALLOWLIST:
        return
    offenders = _package_imports(module, "bricks")
    assert not offenders, (
        f"Framework module {rel} imports brick code (``bricks.*``).  Call brick "
        "scripts through agent_foundry.deploy._brick_registry.resolve_action; if "
        "the import is a deliberate bridge, add it to FRAMEWORK_BRICK_IMPORT_ALLOWLIST "
        "with a justification (issue #714):\n" + "\n".join(f"  L{line}: {target}" for line, target in offenders)
    )


def test_framework_brick_import_allowlist_has_no_stale_entries() -> None:
    """Every allow-list entry must still exist and still import a brick."""
    stale = [
        rel
        for rel in FRAMEWORK_BRICK_IMPORT_ALLOWLIST
        if not (REPO_ROOT / rel).exists() or not _package_imports(REPO_ROOT / rel, "bricks")
    ]
    assert not stale, f"FRAMEWORK_BRICK_IMPORT_ALLOWLIST entries no longer import a brick; remove them: {stale}"
