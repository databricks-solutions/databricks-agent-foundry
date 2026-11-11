"""
Workspace Prerequisites Validator
===================================
Checks that the local environment (and optionally the target Databricks
workspace) meets all requirements for an Agent Bricks deployment.

Two modes:

- ``run_checks(offline=True)``  -- local-only checks (no workspace
  connection needed).  Default in CI's validate step.
- ``run_checks(workspace=True)`` -- offline + workspace connectivity and
  permission checks.

Promoted from ``scripts/validate_prerequisites.py`` to the package
proper as part of issue #96 so the validator can be invoked via
``agent-foundry validate`` from a downstream repo that contains only
``foundry.toml`` and ``instances/``.
"""

from __future__ import annotations

import argparse
import importlib
import shutil
import sys
from pathlib import Path
from typing import Any

from agent_foundry._meta import compare_pinned_sha


class CheckResult:
    """Outcome of a single prerequisite check."""

    def __init__(self, name: str, passed: bool, detail: str = ""):
        self.name = name
        self.passed = passed
        self.detail = detail

    def __str__(self) -> str:
        icon = "PASS" if self.passed else "FAIL"
        msg = f"  [{icon}] {self.name}"
        if self.detail:
            msg += f" -- {self.detail}"
        return msg


# ---------------------------------------------------------------------------
# Offline checks (no workspace required)
# ---------------------------------------------------------------------------


def check_python_version() -> CheckResult:
    v = sys.version_info
    ok = v >= (3, 10)
    return CheckResult(
        "Python >= 3.10",
        ok,
        f"found {v.major}.{v.minor}.{v.micro}",
    )


def check_module(module_name: str, pip_name: str | None = None) -> CheckResult:
    try:
        mod = importlib.import_module(module_name)
        version = getattr(mod, "__version__", "installed")
        return CheckResult(f"Module: {module_name}", True, str(version))
    except ImportError:
        install_hint = pip_name or module_name
        return CheckResult(f"Module: {module_name}", False, f"pip install {install_hint}")


def check_cli_tool(name: str) -> CheckResult:
    path = shutil.which(name)
    if path:
        return CheckResult(f"CLI: {name}", True, path)
    return CheckResult(f"CLI: {name}", False, "not found in PATH")


def check_project_structure(project_root: Path) -> list[CheckResult]:
    """Smoke-check the consumer repo has a ``pyproject.toml``."""
    pyproject = project_root / "pyproject.toml"
    return [
        CheckResult(
            "File: pyproject.toml",
            pyproject.exists(),
            "exists" if pyproject.exists() else "MISSING",
        )
    ]


def check_framework_sha(project_root: Path) -> CheckResult:
    """Fail when the running agent-foundry differs from the repo's pin.

    ``deploy`` bundles the *running* framework build into the app wheel,
    so a mismatch (e.g. a globally-installed CLI shadowing the project
    venv) would ship a framework that does not match ``pyproject.toml``.
    Skipped (passing) when the repo does not pin a git ref or the running
    build has no resolvable SHA (editable / local / wheel install).
    """
    comparison = compare_pinned_sha(project_root)
    return CheckResult(
        "Framework SHA matches pyproject pin",
        comparison.matches,
        comparison.message,
    )


def check_legacy_layout(project_root: Path) -> CheckResult:
    """Fail when config files of the removed legacy layout remain."""
    from agent_foundry._legacy_layout import LegacyLayoutError, reject_legacy_layout

    try:
        reject_legacy_layout(project_root, project_root / "instances")
    except LegacyLayoutError as exc:
        return CheckResult("Config layout", False, str(exc))
    return CheckResult("Config layout", True, "foundry.toml")


def discover_instance_dirs(project_root: Path) -> list[Path]:
    """Return application instance directories declared by ``foundry.toml``.

    Uses the repo-root ``[[instances]]`` entries, falling back to an
    ``instances/*/foundry.toml`` scan when the repo-root file is missing
    or invalid.
    """
    from agent_foundry._instance_marker import is_instance_dir
    from agent_foundry.loader import load_repo_config

    root = project_root.resolve()
    try:
        repo = load_repo_config(root)
        dirs = [(root / entry.path).resolve() for entry in repo.instances]
        return sorted((d for d in dirs if d.is_dir()), key=lambda p: p.name)
    except Exception:
        pass
    instances_root = root / "instances"
    if not instances_root.is_dir():
        return []
    return sorted((d for d in instances_root.iterdir() if d.is_dir() and is_instance_dir(d)), key=lambda p: p.name)


def check_brick_registry() -> list[CheckResult]:
    """Verify every registered brick adapter loads from the installed package."""
    from agent_foundry.bricks import iter_bricks

    try:
        bricks = iter_bricks()
    except Exception as exc:
        return [CheckResult("Brick registry", False, str(exc))]
    return [CheckResult(f"Brick: {brick.display_name}", True, f"registered ({brick.name})") for brick in bricks]


def check_instance_bricks(
    instance_dir: Path,
    *,
    target: str,
    project_root: Path,
) -> list[CheckResult]:
    """Report which registered bricks apply to one instance directory."""
    from agent_foundry.bricks import DeployContext, iter_bricks, load_brick_variables

    try:
        variables = load_brick_variables(project_root, target, instance_dir)
    except Exception as exc:
        return [
            CheckResult(
                f"Instance: {instance_dir.name}",
                False,
                f"could not load variables: {exc}",
            )
        ]

    from agent_foundry.loader import load_instance_config

    try:
        config = load_instance_config(
            project_root,
            instance=instance_dir.name,
            target_name=target,
        )
    except Exception:
        config = None

    ctx = DeployContext(
        instance_dir=instance_dir,
        target=target,
        variables=variables,
        config=config,
    )
    results: list[CheckResult] = []
    for brick in iter_bricks():
        try:
            applies = brick.applies_to(ctx)
        except Exception as exc:
            results.append(CheckResult(f"Brick: {brick.display_name}", False, str(exc)))
            continue
        results.append(
            CheckResult(
                f"Brick: {brick.display_name}",
                True,
                "configured" if applies else "not configured",
            )
        )
    return results


def check_instance_payloads(instance_dir: Path) -> list[CheckResult]:
    """Run each applicable payload adapter's optional ``validate_instance`` hook.

    The hook is optional so adapters registered by third parties keep
    satisfying the ``AppPayloadAdapter`` protocol without it.
    """
    from agent_foundry.deploy.payload_adapters import iter_payload_adapters

    results: list[CheckResult] = []
    for adapter in iter_payload_adapters():
        validate_instance = getattr(adapter, "validate_instance", None)
        if validate_instance is None or not adapter.applies_to(instance_dir, {}):
            continue
        try:
            errors = list(validate_instance(instance_dir))
        except Exception as exc:
            errors = [str(exc)]
        results.append(CheckResult(f"Payload: {adapter.name}", not errors, "; ".join(errors) or "valid"))
    return results


def summarize_brick_registry(results: list[CheckResult]) -> CheckResult:
    """Collapse per-brick registry rows into one framework summary line."""
    if not results:
        return CheckResult("Brick adapters", False, "none registered")
    failures = [r for r in results if not r.passed]
    if failures:
        return CheckResult("Brick adapters", False, failures[0].detail)
    return CheckResult("Brick adapters", True, f"{len(results)} registered")


def check_instances(project_root: Path) -> CheckResult:
    instances = discover_instance_dirs(project_root)
    if not instances:
        return CheckResult("Application instances", True, "none configured (OK)")
    return CheckResult(
        "Application instances",
        True,
        f"{len(instances)} found: {', '.join(d.name for d in instances)}",
    )


# ---------------------------------------------------------------------------
# Workspace checks (require connectivity)
# ---------------------------------------------------------------------------


def check_workspace_connection(variables: dict[str, str] | None = None) -> tuple[CheckResult, Any]:
    try:
        from agent_foundry.workspace_client import get_workspace_client

        client = get_workspace_client(variables)
        me = client.current_user.me()
        return (
            CheckResult("Workspace auth", True, f"authenticated as {me.user_name}"),
            client,
        )
    except Exception as exc:
        return CheckResult("Workspace auth", False, str(exc)), None


def check_warehouse(client: Any, warehouse_id: str) -> CheckResult:
    if not warehouse_id:
        return CheckResult("SQL Warehouse", False, "no warehouse_id configured")
    try:
        wh = client.warehouses.get(warehouse_id)
        return CheckResult(
            "SQL Warehouse",
            True,
            f"{wh.name} ({wh.state.value if wh.state else 'unknown'})",
        )
    except Exception as exc:
        return CheckResult("SQL Warehouse", False, str(exc))


def check_unity_catalog(client: Any) -> CheckResult:
    try:
        catalogs = list(client.catalogs.list())
        return CheckResult("Unity Catalog", True, f"{len(catalogs)} catalog(s) accessible")
    except Exception as exc:
        return CheckResult("Unity Catalog", False, str(exc))


def check_workspace_path(client: Any, deployer: str) -> CheckResult:
    parent = f"/Workspace/Users/{deployer}"
    try:
        client.workspace.get_status(parent)
        return CheckResult("Workspace path", True, parent)
    except Exception:
        return CheckResult("Workspace path", False, f"{parent} not accessible")


# ---------------------------------------------------------------------------
# Variable loading
# ---------------------------------------------------------------------------


def _load_target_variables(project_root: Path, target: str) -> dict[str, str]:
    """Lightweight variable loading for the validator.

    Delegates to :func:`agent_foundry.workspace_client.load_target_auth_variables`
    so ``validate --workspace`` and ``agent-foundry test`` share one
    foundry.toml resolution path (#432).
    """
    from agent_foundry.workspace_client import load_target_auth_variables

    return load_target_auth_variables(project_root, target)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_checks(
    *,
    offline: bool = True,
    workspace: bool = False,
    target: str | None = None,
    project_root: Path | None = None,
) -> int:
    """Run the configured prerequisite checks.

    Parameters
    ----------
    offline:
        Always-on local environment + project structure checks.
        Currently always runs; the flag is kept so future callers can
        opt out symmetrically with ``workspace``.
    workspace:
        Add live workspace connectivity / permission checks (requires
        ``databricks auth login`` or env vars).
    target:
        Target name used to load variables (``[targets.<target>]`` in
        ``foundry.toml``) when ``workspace=True``.
    project_root:
        Repo root used to locate ``pyproject.toml``, ``foundry.toml`` and
        ``instances/``.  Defaults to the current working directory; pass
        explicitly when invoking from outside the repo root.

    Returns
    -------
    int
        ``0`` if every check passed, ``1`` otherwise.  Output is human-
        readable text on stdout.
    """
    root = (project_root or Path.cwd()).resolve()

    print("=== Agent Bricks: Validate ===\n")
    all_results: list[CheckResult] = []

    if offline or not workspace:
        print("Prerequisites:")
        prereq_results: list[CheckResult] = [check_python_version()]
        for mod, pip in [("yaml", "pyyaml"), ("databricks.sdk", "databricks-sdk")]:
            prereq_results.append(check_module(mod, pip))
        if workspace:
            prereq_results.append(check_cli_tool("databricks"))
        all_results.extend(prereq_results)
        for r in prereq_results:
            print(r)

        print("\nProject:")
        struct_results = check_project_structure(root)
        inst = check_instances(root)
        project_results = [*struct_results, check_legacy_layout(root), inst]
        all_results.extend(project_results)
        for r in project_results:
            print(r)

        registry_results = check_brick_registry()
        framework_summary = summarize_brick_registry(registry_results)
        all_results.append(framework_summary)
        sha_result = check_framework_sha(root)
        all_results.append(sha_result)
        print("\nFramework:")
        print(framework_summary)
        print(sha_result)

        target_name = target or "dev"
        for instance_dir in discover_instance_dirs(root):
            print(f"\nInstance: {instance_dir.name} ({target_name}):")
            brick_results = check_instance_bricks(
                instance_dir,
                target=target_name,
                project_root=root,
            )
            brick_results += check_instance_payloads(instance_dir)
            all_results.extend(brick_results)
            for r in brick_results:
                print(r)

    if workspace:
        print("\nWorkspace Connectivity:")
        variables = _load_target_variables(root, target or "dev")

        auth_result, client = check_workspace_connection(variables)
        all_results.append(auth_result)
        print(auth_result)

        if client:
            wh_id = variables.get("warehouse_id", "")
            wh_result = check_warehouse(client, wh_id)
            all_results.append(wh_result)
            print(wh_result)

            uc_result = check_unity_catalog(client)
            all_results.append(uc_result)
            print(uc_result)

            try:
                me = client.current_user.me()
                wp_result = check_workspace_path(client, me.user_name)
                all_results.append(wp_result)
                print(wp_result)
            except Exception:
                pass

    failures = [r for r in all_results if not r.passed]
    print(f"\n{'=' * 40}")
    if failures:
        print(f"{len(failures)} check(s) FAILED:")
        for f in failures:
            print(f"  - {f.name}: {f.detail}")
        return 1
    print(f"All {len(all_results)} checks passed.")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Thin argparse wrapper for direct invocation (kept for parity)."""
    parser = argparse.ArgumentParser(description="Validate Agent Bricks environment and configuration")
    parser.add_argument("--workspace", action="store_true", help="Include workspace connectivity checks")
    parser.add_argument("--target", default="dev", help="Target to load variables from")
    parser.add_argument("--offline", action="store_true", help="Run only offline checks (the default)")
    args = parser.parse_args(argv)

    return run_checks(
        offline=True,
        workspace=args.workspace,
        target=args.target,
    )


if __name__ == "__main__":
    raise SystemExit(main())
