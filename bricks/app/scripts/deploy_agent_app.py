"""
Deploy Bundled Agent App
=========================
Creates (or updates) a Databricks App that hosts both the agent backend
and the ``e2e-chatbot-app-next`` chat UI in a single process.

This module is the App-host deploy entrypoint.  It is payload-agnostic:
it resolves the payload selected by ``payloads/<payload>/payload.yml``
(via the generic
:func:`~agent_foundry.deploy.payload_adapters.load_payload_config`) and
drives the generic App-host engine in
:mod:`agent_foundry.deploy.app_host` (issue #337) through the
:class:`~agent_foundry.deploy.payload_adapters.AppPayloadAdapter` seam.
All payload-specific glue (runtime/infra specs, the chat-history Lakebase
hook) lives in the payload package, not here.

Flow:
  1. Build a wheel-based deploy staging dir
     (``agent_foundry.deploy.staging.build_deploy_staging``) -- ships
     the framework as a built wheel + ``requirements.txt`` + the
     instance dir, instead of syncing the entire source tree.
  2. ``databricks sync <staging> <workspace_path> --full`` -- sync the
     staging dir to the workspace.
  3. Resolve ``${var.*}`` placeholders in the instance's
     ``app/app.yaml`` and upload the result to the workspace root.
  4. Reconcile the app *spec* (compute size, resources, OBO scopes)
     via the typed SDK -- create when absent, else async
     ``create_update`` (supports compute resize).
  5. Grant SP permissions, then deploy the app from the synced
     workspace path.

Usage::

    python bricks/app/scripts/deploy_agent_app.py \\
        --instance <your-app> \\
        --target dev \\
        [--app-name my-agent-app] \\
        [--dry-run]

``--instance`` is a bare name resolved under ``instances/<name>/`` (same
convention as ``scripts/deploy_all.py`` and ``run_evaluation.py``).
``--app-name`` defaults to the instance's computed
``app_resource_name`` (e.g. ``dev-<app_name>-app``); pass it explicitly
only to override.

Requires:
    - ``databricks-sdk``
    - ``databricks`` CLI on ``PATH``
    - ``build`` (``[project.optional-dependencies].dev``) -- for the
      ``python -m build`` invocation in the staging helper
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from databricks.sdk import WorkspaceClient

from agent_foundry._instance_marker import is_instance_dir
from agent_foundry.bricks import fill_state_outputs, load_brick_variables

# App-host engine (issue #337): the generic deploy/destroy orchestrators
# this module delegates to.
from agent_foundry.deploy.app_host import deploy_on_host, destroy_on_host

# The agent payload's deploy glue is NOT re-exported here: this host script
# is payload-agnostic and selects the payload via the generic
# ``load_payload_config`` -> ``AppPayloadAdapter`` seam (issue #338).
__all__ = ["deploy", "destroy", "main"]


def _get_workspace_client(
    variables: dict[str, str] | None = None,
    *,
    ws: WorkspaceClient | None = None,
    construct: bool = False,
) -> WorkspaceClient | None:
    if ws is not None:
        return ws
    if not construct:
        return None
    from agent_foundry.workspace_client import prefer_workspace_client

    return prefer_workspace_client(variables)


def deploy(
    app_name: str,
    target: str,
    variables: dict[str, str],
    *,
    instance_dir: Path | None = None,
    dry_run: bool = False,
    ws: WorkspaceClient | None = None,
) -> int:
    """Main deploy entry point. Returns 0 on success.

    Resolves the App payload selected by ``app/payload.yml`` (the agent
    payload by default) and hands it, with the resolved workspace client
    and deployer, to the generic
    :func:`agent_foundry.deploy.app_host.deploy_on_host` orchestrator.
    The orchestrator owns the fixed deploy sequence; the payload adapter
    owns the agent-specific inputs (runtime spec, infra spec, the
    Lakebase heal hook).

    Parameters
    ----------
    instance_dir:
        Path to the application instance directory.  The instance's
        ``app/app.yaml`` is resolved (``${var.*}`` substitution) and
        uploaded to the workspace root so the Databricks App runtime
        reads the correct ``INSTANCE_DIR`` and other env vars.
    """
    from agent_foundry.deploy.payload_adapters import get_payload_adapter, load_payload_config

    if not dry_run:
        ws = _get_workspace_client(variables, ws=ws)

    deployer = variables.get("deployer", "")
    if not deployer and ws:
        # ``User.user_name`` is typed as ``str | None`` in the SDK
        # stubs (the directory always populates it for real users,
        # but service-principals can have an empty user_name); fall
        # back to "" so ``deployer: str`` stays valid.
        deployer = ws.current_user.me().user_name or ""
        print(f"  Resolved deployer: {deployer}")

    payload_cfg = load_payload_config(instance_dir)
    payload = get_payload_adapter(payload_cfg.adapter)

    return deploy_on_host(
        payload,
        ws=ws,
        app_name=app_name,
        target=target,
        variables=variables,
        instance_dir=instance_dir,
        deployer=deployer,
        spec=payload_cfg.spec,
        dry_run=dry_run,
    )


def destroy(
    app_name: str,
    target: str,
    variables: dict[str, str],
    *,
    instance_dir: Path | None = None,
    dry_run: bool = False,
    ws: WorkspaceClient | None = None,
) -> int:
    """Delete the Databricks App, its workspace sync directory, and the
    published App-URL state.

    Thin wrapper over :func:`agent_foundry.deploy.app_host.destroy_on_host`
    -- the teardown itself is generic, so the engine owns it (issue #337).
    Payload-specific teardown (Lakebase chat history) is handled by the
    App brick around this call.
    """
    sol_root = variables.get("solution_root", "")
    if not dry_run:
        ws = _get_workspace_client(variables, ws=ws)
    return destroy_on_host(
        ws,
        app_name,
        target,
        instance_dir=instance_dir,
        solution_root=sol_root,
        dry_run=dry_run,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deploy bundled agent Databricks App")
    parser.add_argument(
        "--instance",
        required=True,
        help="Instance name (resolved under instances/<name>/)",
    )
    parser.add_argument("--target", default="dev", help="Deployment target")
    parser.add_argument(
        "--app-name",
        default=None,
        help=(
            "App name. Defaults to the instance's computed "
            "``app_resource_name`` (typically ``<target>-<app_name>-app``)."
        ),
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=None,
        help="Repo root containing foundry.toml and instances/. Defaults to CWD.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--vars", nargs="*", default=[])
    args = parser.parse_args(argv)

    project_root = args.project_root or Path.cwd()

    instance_dir = project_root / "instances" / args.instance
    if not is_instance_dir(instance_dir):
        print(
            f"Error: Instance directory not found: {instance_dir}",
            file=sys.stderr,
        )
        return 1

    variables = load_brick_variables(project_root, args.target, instance_dir, extra_vars=args.vars)

    app_name = args.app_name or variables.get("app_resource_name") or variables.get("app_name") or instance_dir.name

    ws = _get_workspace_client(variables, construct=True)
    variables = fill_state_outputs(variables, instance_dir, args.target, ws)
    return deploy(
        app_name,
        args.target,
        variables,
        instance_dir=instance_dir,
        dry_run=args.dry_run,
        ws=ws,
    )


if __name__ == "__main__":
    raise SystemExit(main())
