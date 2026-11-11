"""``agent-foundry deploy`` and the deploy helpers shared with other verbs."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer

from agent_foundry.config import InstanceConfig
from agent_foundry.deploy._orchestrator import (
    deploy_instance,
    resolve_deployer,
    workspace_client_for_run,
)
from agent_foundry.public import DeployResult

if TYPE_CHECKING:
    from agent_foundry.public import FoundryRepoConfig


def pytest_subprocess_env(
    cwd: Path,
    target: str,
    variables: dict[str, str] | None = None,
) -> dict[str, str]:
    """Env for a pytest child: target profile, host, and project root (#432).

    ``export_mlflow_auth_env`` uses ``setdefault`` so an explicit caller
    env still wins. ``AGENT_FOUNDRY_PROJECT_ROOT`` is always the cwd the
    CLI was invoked from — the runtime refuses to guess.
    """
    from agent_foundry import env as af_env
    from agent_foundry.workspace_client import export_mlflow_auth_env, load_target_auth_variables

    vars_ = variables if variables is not None else load_target_auth_variables(cwd, target)
    export_mlflow_auth_env(vars_)
    child = {
        **dict(os.environ),
        af_env.AGENT_FOUNDRY_TARGET.name: target,
        af_env.AGENT_FOUNDRY_PROJECT_ROOT.name: str(cwd.resolve()),
    }
    host = vars_.get("host", "")
    if host:
        child[af_env.DATABRICKS_HOST.name] = host
    warehouse_id = vars_.get("warehouse_id", "")
    if warehouse_id:
        # Consumed by tests/conftest only; not in the framework registry.
        child["TEST_WAREHOUSE_ID"] = warehouse_id
    return child


def _run_tests(
    cwd: Path,
    variables: dict[str, str],
    run_benchmarks: bool,
    *,
    target: str,
) -> int:
    """Run the test suite against the deployed resources."""
    subprocess_env = pytest_subprocess_env(cwd, target, variables)

    print("\n--- Running offline tests ---")
    cmd = [sys.executable, "-m", "pytest", "-v", "--tb=short"]
    result = subprocess.run(cmd, cwd=str(cwd), env=subprocess_env)
    if result.returncode != 0:
        print("Offline tests FAILED")
        return result.returncode

    print("\n--- Running live tests ---")
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "--run-live",
        "-v",
        "--tb=short",
    ]
    if not run_benchmarks:
        cmd.extend(["-k", "not test_benchmarks"])

    result = subprocess.run(cmd, cwd=str(cwd), env=subprocess_env)
    if result.returncode != 0:
        print("Live tests FAILED")
        return result.returncode

    print("All tests PASSED")
    return 0


def _brick_name_parts() -> set[str]:
    """Return every lowercase name substring a brick can be matched on.

    Mirrors the ``haystack_parts`` used by the orchestrator's ``--skip`` /
    ``--only`` filter so CLI-side validation and runtime filtering agree.
    """
    from agent_foundry.bricks import iter_bricks
    from agent_foundry.deploy._brick_registry import cli_name_for

    return {
        part
        for brick in iter_bricks()
        for part in (
            getattr(brick, "display_name", brick.name).lower(),
            brick.name.lower(),
            cli_name_for(brick.name).lower(),
        )
    }


def _deployable_brick_names() -> list[str]:
    """Canonical CLI handle per brick -- used for the ``--only`` error hint.

    Unlike :func:`_brick_name_parts` (every matchable alias, used for the
    substring check) this returns exactly one name per registered brick so
    the error message is a clean, unambiguous list rather than a dump of
    display-name/registry/CLI spellings.
    """
    from agent_foundry.bricks import iter_bricks
    from agent_foundry.deploy._brick_registry import cli_name_for

    return sorted(cli_name_for(brick.name) for brick in iter_bricks())


def _fail_on_framework_sha_mismatch(project_root: Path) -> int:
    """Abort deploy when the running agent-foundry differs from the pin.

    ``deploy`` bundles the *running* framework into the app wheel, so a
    mismatch would ship a framework that does not match the repo's
    ``pyproject.toml``. Returns ``0`` when the pin matches (or the
    comparison is skipped); ``1`` after printing the error.
    """
    from agent_foundry._meta import compare_pinned_sha

    comparison = compare_pinned_sha(project_root)
    if comparison.matches:
        return 0
    bar = "!" * 78
    print(f"\n{bar}")
    print("  ERROR: agent-foundry version mismatch")
    print(f"  {comparison.message}")
    print(f"{bar}\n")
    return 1


def _validate_only_bricks(only_bricks: set[str]) -> int:
    """Fail fast when a ``--only`` token matches no registered brick.

    Without this guard a typo'd token would silently filter out every
    brick, producing a no-op deploy. Returns ``0`` when all tokens match
    at least one brick, otherwise ``1`` after printing the offending
    tokens and the valid brick names.
    """
    known = _brick_name_parts()
    unmatched = sorted(token for token in only_bricks if not any(token in part for part in known))
    if unmatched:
        print(f"\nERROR: --only matched no brick for: {', '.join(unmatched)}")
        print(f"  Valid bricks: {', '.join(_deployable_brick_names())}")
        return 1
    return 0


def _run_deploy(
    *,
    instance: str | None,
    target: str,
    instances_root: Path | None,
    dry_run: bool,
    run_tests: bool,
    skip_benchmarks: bool,
    skip: str,
    only: str,
    extra_vars: list[str],
    offline: bool = False,
) -> int:
    """Body of the ``deploy`` verb, kept testable apart from typer.

    Reads the repo-root ``foundry.toml`` + per-instance ``foundry.toml``
    files; leftover legacy config files abort the run (see
    :func:`agent_foundry._legacy_layout.reject_legacy_layout`).
    """
    from agent_foundry._legacy_layout import LegacyLayoutError, reject_legacy_layout

    cwd = Path.cwd()
    instances_root_p = instances_root or (cwd / "instances")
    skip_bricks = {s.strip().lower() for s in skip.split(",") if s.strip()}
    only_bricks = {s.strip().lower() for s in only.split(",") if s.strip()}

    if offline and not dry_run:
        print("\nERROR: --offline requires --dry-run (offline is a hermetic plan; it cannot mutate).")
        return 1

    if skip_bricks and only_bricks:
        print("\nERROR: --skip and --only are mutually exclusive.")
        return 1

    if only_bricks:
        rc = _validate_only_bricks(only_bricks)
        if rc != 0:
            return rc

    print(f"=== Agent Bricks: Deploy All ({target}) ===")
    if dry_run and offline:
        print("  Mode: DRY RUN (offline, no workspace)")
    elif dry_run:
        print("  Mode: DRY RUN (may read live workspace state)")
    elif offline:
        print("  Mode: OFFLINE")
    print(f"  Instances root: {instances_root_p}")
    print()

    try:
        reject_legacy_layout(cwd, instances_root_p)
    except LegacyLayoutError as exc:
        print(f"\nERROR: {exc}")
        return 1

    if _fail_on_framework_sha_mismatch(cwd) != 0:
        return 1

    return _run_deploy_foundry_toml(
        repo_root=cwd,
        instance=instance,
        target=target,
        dry_run=dry_run,
        run_tests=run_tests,
        skip_benchmarks=skip_benchmarks,
        skip_bricks=skip_bricks,
        only_bricks=only_bricks,
        extra_vars=extra_vars,
        offline=offline,
    )


def _run_deploy_foundry_toml(
    *,
    repo_root: Path,
    instance: str | None,
    target: str,
    dry_run: bool,
    run_tests: bool,
    skip_benchmarks: bool,
    skip_bricks: set[str],
    only_bricks: set[str],
    extra_vars: list[str],
    offline: bool = False,
) -> int:
    """New path: repo-root ``foundry.toml`` + per-instance ``foundry.toml``.

    Single source of truth for the variable bag is
    :meth:`InstanceConfig.to_variables`, populated by
    :func:`agent_foundry.loader.load_instance_config`.  The deployer
    field is resolved once at the top and folded in via
    ``extra_vars`` so the loader's CLI-overrides handling stays the
    only entry point for ``${var.*}`` mutations.
    """
    from agent_foundry.loader import (
        FoundryConfigError,
        load_instance_config,
        load_repo_config,
    )

    # The deployer is resolved from each InstanceConfig's variable bag
    # in the validate pass below.
    try:
        repo = load_repo_config(repo_root)
    except FoundryConfigError as exc:
        print(f"ERROR: {exc}")
        return 1

    # Determine which instances to deploy.
    if instance:
        # Loader.load_instance_config will validate the name exists.
        instance_names = [instance]
    else:
        instance_names = [_instance_name_from_ref(repo_root, ref) for ref in repo.instances]

    if not instance_names:
        print(
            f"No [[instances]] entries declared in {repo_root / 'foundry.toml'}. "
            "Add at least one instance reference before deploying."
        )
        return 0

    # Validate-pass: load each InstanceConfig up front so config
    # errors surface before any side-effecting brick runs.
    instance_configs: dict[str, InstanceConfig] = {}
    ws: Any | None = None
    for inst_name in instance_names:
        try:
            cfg = load_instance_config(
                repo_root,
                instance=inst_name,
                target_name=target,
                extra_vars=extra_vars or None,
            )
        except FoundryConfigError as exc:
            print(f"  [FAIL] {inst_name} config validation: {exc}")
            return 1
        except Exception as exc:
            print(f"  [FAIL] {inst_name} config validation: {exc}")
            return 1

        # Resolve the deployer using the variable bag we just built,
        # then re-compose with the resolved deployer folded in so
        # bricks see it via ${var.deployer}.
        bag = cfg.to_variables()
        if ws is None:
            ws = workspace_client_for_run(bag, offline=offline)
        bag = resolve_deployer(bag, dry_run=dry_run, offline=offline, ws=ws)
        if bag.get("deployer"):
            cfg = load_instance_config(
                repo_root,
                instance=inst_name,
                target_name=target,
                extra_vars=(extra_vars or []) + [f"deployer={bag['deployer']}"],
            )

        instance_configs[inst_name] = cfg
        print(f"  [OK] {inst_name} config validated")

    # CLI ``--vars`` extras that aren't first-class target/instance
    # fields still need to reach the bricks via ``${var.*}``.  The
    # loader applies overrides to the typed fields it knows about
    # (host, catalog, schema, ...) and ignores the rest; we fold
    # those extras into the final variable bag here so free-form
    # user-defined variables keep working.
    from agent_foundry.loader import _parse_extra_vars as _parse_vars

    extras = _parse_vars(extra_vars)

    t0 = time.time()
    all_instance_results: dict[str, DeployResult] = {}
    last_variables: dict[str, str] = {}

    for inst_name, cfg in instance_configs.items():
        instance_dir = instance_dir_for(repo_root, repo, inst_name)
        variables = cfg.to_variables()
        # Free-form extras win over to_variables() keys with the same
        # name: ``--vars k=v`` is last-wins.
        variables.update(extras)
        try:
            deploy_result = deploy_instance(
                instance_dir,
                target,
                variables=variables,
                dry_run=dry_run,
                instance_config=cfg,
                skip_bricks=skip_bricks,
                only_bricks=only_bricks,
                ws=ws,
                offline=offline,
            )
        except FoundryConfigError as exc:
            print(f"\nERROR: {exc}")
            return 1
        all_instance_results[inst_name] = deploy_result
        last_variables = variables

    elapsed = time.time() - t0
    return _summarise_and_test(
        cwd=repo_root,
        all_instance_results=all_instance_results,
        last_variables=last_variables,
        target=target,
        elapsed=elapsed,
        run_tests=run_tests,
        skip_benchmarks=skip_benchmarks,
    )


def _instance_name_from_ref(repo_root: Path, ref: Any) -> str:
    """Return the instance name (last path segment) for a
    ``[[instances]]`` reference.

    Matches the naming convention F-PR2's loader uses internally:
    the instance name is the basename of its directory.
    """
    return Path(ref.path).name


def instance_dir_for(repo_root: Path, repo: FoundryRepoConfig, instance_name: str) -> Path:
    """Resolve ``instance_name`` to its directory using the repo's
    declared ``[[instances]]`` list.

    Raises if the name doesn't match any entry -- but this should
    never happen because the validation pass in
    :func:`_run_deploy_foundry_toml` already exercised every name
    via the loader.
    """
    for ref in repo.instances:
        if Path(ref.path).name == instance_name:
            return (repo_root / ref.path).resolve()
    raise RuntimeError(
        f"Instance {instance_name!r} not declared in foundry.toml. "
        "This is a framework bug -- the validation pass should have caught it."
    )


def _summarise_and_test(
    *,
    cwd: Path,
    all_instance_results: dict[str, DeployResult],
    last_variables: dict[str, str],
    target: str,
    elapsed: float,
    run_tests: bool,
    skip_benchmarks: bool,
) -> int:
    """Shared post-deploy summary + optional test runner.

    Extracted in F-PR5 so the two deploy paths (legacy YAML and the
    new ``foundry.toml`` loader) don't drift on summary formatting
    or the ``--run-tests`` semantics.
    """

    print(f"\n{'=' * 50}")
    print(f"Deployment Summary ({target}, {elapsed:.1f}s)")
    print(f"{'=' * 50}")

    has_errors = False
    for inst_name, deploy_result in all_instance_results.items():
        print(f"\n  {inst_name}/")
        for brick_name, results in deploy_result.bricks.items():
            for r in results:
                status = r.status
                icon = "OK" if status == "ok" else ("SKIP" if status == "skipped" else "FAIL")
                if "error" in status:
                    has_errors = True
                print(f"    [{icon}] {brick_name}/{r.name}")

    if has_errors:
        print("\nDeployment completed with errors.")
        if not run_tests:
            return 1

    if run_tests:
        print(f"\n{'=' * 50}")
        print("Running Tests")
        print(f"{'=' * 50}")
        rc = _run_tests(
            cwd,
            last_variables,
            run_benchmarks=not skip_benchmarks,
            target=target,
        )
        if rc != 0:
            return rc

    return 1 if has_errors else 0


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def deploy_cmd(
    instance: str | None = typer.Option(
        None,
        "--instance",
        help="Deploy only this instance (directory name under --instances-root).",
    ),
    target: str = typer.Option(
        "dev",
        "--target",
        help="Deployment target (dev, staging, prod).",
    ),
    instances_root: Path | None = typer.Option(
        None,
        "--instances-root",
        help="Directory containing instance subdirs. Defaults to <cwd>/instances.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help=(
            "Assemble and validate without mutating. Still reads live workspace "
            "state (Genie/MCP destroy plans) unless --offline."
        ),
    ),
    offline: bool = typer.Option(
        False,
        "--offline",
        help="Hermetic plan: never construct a WorkspaceClient. Requires --dry-run.",
    ),
    run_tests: bool = typer.Option(
        False,
        "--run-tests",
        help="Run test suite after deployment (offline + live).",
    ),
    skip_benchmarks: bool = typer.Option(
        False,
        "--skip-benchmarks",
        help="Skip benchmark tests (faster, still runs lifecycle tests).",
    ),
    skip: str = typer.Option(
        "",
        "--skip",
        help="Comma-separated brick names to skip (e.g. 'app,observability').",
    ),
    only: str = typer.Option(
        "",
        "--only",
        help=(
            "Comma-separated brick names to deploy exclusively (e.g. "
            "'warehouse,skills'). Substring match like --skip, so '--only "
            "vector' also selects 'vector_search'. Mutually exclusive with --skip."
        ),
    ),
    extra_vars: list[str] | None = typer.Option(
        None,
        "--vars",
        help="Extra variable as key=value (repeat for multiple).",
    ),
) -> None:
    """Deploy all Agent Bricks for one or all instances."""
    rc = _run_deploy(
        instance=instance,
        target=target,
        instances_root=instances_root,
        dry_run=dry_run,
        run_tests=run_tests,
        skip_benchmarks=skip_benchmarks,
        skip=skip,
        only=only,
        extra_vars=extra_vars or [],
        offline=offline,
    )
    raise typer.Exit(code=rc)


def register(app: typer.Typer) -> None:
    """Add this module's commands to the root *app*."""
    app.command("deploy")(deploy_cmd)
