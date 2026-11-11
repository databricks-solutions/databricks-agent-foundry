"""Deploy Databricks Jobs declared by ``jobs/<name>/job.yml``."""

from __future__ import annotations

import argparse
import io
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_foundry.bricks import fill_state_outputs, load_brick_variables, resolve_sub_instances
from agent_foundry.config import merge_managed_by_tags
from agent_foundry.state import delete_state, load_state, save_state
from agent_foundry.variables import add_project_root_arg
from bricks.job.scripts.validate_job import load_job_config


@dataclass(frozen=True)
class SourceUpload:
    local_path: Path
    remote_path: str
    is_notebook: bool


def _safe_segment(value: str, label: str) -> str:
    if not value or value in {".", ".."} or "/" in value or "\\" in value:
        raise ValueError(f"Invalid {label}: {value!r}")
    return value


def _local_source(job_dir: Path, configured_path: str) -> Path | None:
    if not configured_path.startswith("./"):
        return None
    candidate = (job_dir / configured_path[2:]).resolve()
    root = job_dir.resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError(f"Task source escapes job directory: {configured_path}")
    if not candidate.is_file():
        raise FileNotFoundError(f"Task source not found: {candidate}")
    return candidate


def _remote_path(workspace_root: str, job_dir: Path, local: Path, *, keep_suffix: bool) -> str:
    """Mirror *local*'s position under the job directory into the workspace.

    Keeping the relative directory means two same-named files in
    different folders no longer land on the same workspace path.
    Notebooks are addressed without their extension.
    """
    relative = local.relative_to(job_dir.resolve())
    if not keep_suffix:
        relative = relative.with_suffix("")
    return f"{workspace_root}/{relative.as_posix()}"


def _collect_task_sources(
    task: dict[str, Any],
    job_dir: Path,
    workspace_root: str,
    uploads: list[SourceUpload],
) -> None:
    """Rewrite *task*'s ``./``-relative sources and record their uploads.

    Recurses into ``for_each_task`` so a fan-out body is handled like a
    top-level task (#406).
    """
    notebook = task.get("notebook_task")
    if notebook:
        local = _local_source(job_dir, notebook["notebook_path"])
        if local is not None:
            remote = _remote_path(workspace_root, job_dir, local, keep_suffix=False)
            notebook["notebook_path"] = remote
            notebook["source"] = "WORKSPACE"
            uploads.append(SourceUpload(local, remote, True))

    spark_python = task.get("spark_python_task")
    if spark_python:
        local = _local_source(job_dir, spark_python["python_file"])
        if local is not None:
            remote = _remote_path(workspace_root, job_dir, local, keep_suffix=True)
            # ``python_file`` is the one source the API addresses through
            # the ``/Workspace`` mount; notebooks and SQL files are not.
            spark_python["python_file"] = f"/Workspace{remote}"
            spark_python["source"] = "WORKSPACE"
            uploads.append(SourceUpload(local, remote, False))

    sql_file = (task.get("sql_task") or {}).get("file")
    if sql_file:
        local = _local_source(job_dir, sql_file["path"])
        if local is not None:
            remote = _remote_path(workspace_root, job_dir, local, keep_suffix=True)
            sql_file["path"] = remote
            sql_file["source"] = "WORKSPACE"
            uploads.append(SourceUpload(local, remote, False))

    for_each = task.get("for_each_task")
    if for_each:
        _collect_task_sources(for_each["task"], job_dir, workspace_root, uploads)


def _dedupe_uploads(uploads: list[SourceUpload]) -> list[SourceUpload]:
    """Collapse repeat references to one file; reject genuine clashes.

    Reusing the same notebook from several tasks is normal and should
    upload once.  Two *different* files claiming one workspace path
    (``report.py`` and ``report.sql``, whose notebook paths both drop
    the suffix) would silently overwrite each other instead.
    """
    by_remote: dict[str, SourceUpload] = {}
    for upload in uploads:
        seen = by_remote.setdefault(upload.remote_path, upload)
        if seen.local_path != upload.local_path:
            raise ValueError(
                f"Two task sources map to the same workspace path {upload.remote_path}: "
                f"{seen.local_path} and {upload.local_path}"
            )
    return list(by_remote.values())


def build_job_settings(
    job_dir: Path,
    target: str,
    variables: dict[str, str],
) -> tuple[dict[str, Any], list[SourceUpload]]:
    """Build a Jobs API settings payload and source upload plan."""
    config = load_job_config(job_dir, variables)
    settings = config.model_dump(exclude_none=True)
    extra_tags = settings.get("tags") if isinstance(settings.get("tags"), dict) else {}
    settings["tags"] = merge_managed_by_tags(variables.get("app_name"), extra_tags)

    app_name = _safe_segment(
        variables.get("app_name", job_dir.parent.parent.name),
        "app_name",
    )
    target_name = _safe_segment(target, "target")
    job_name = _safe_segment(job_dir.name, "job directory name")
    workspace_root = f"/Shared/agent-foundry/{target_name}/{app_name}/jobs/{job_name}"

    uploads: list[SourceUpload] = []
    for task in settings["tasks"]:
        _collect_task_sources(task, job_dir, workspace_root, uploads)

    return settings, _dedupe_uploads(uploads)


def _upload_sources(ws: Any, uploads: list[SourceUpload]) -> None:
    from databricks.sdk.service.workspace import ImportFormat, Language

    for upload in uploads:
        ws.workspace.mkdirs(upload.remote_path.rsplit("/", 1)[0])
        kwargs: dict[str, Any] = {
            "path": upload.remote_path,
            "content": io.BytesIO(upload.local_path.read_bytes()),
            "overwrite": True,
        }
        if upload.is_notebook:
            suffix = upload.local_path.suffix.lower()
            languages = {
                ".py": Language.PYTHON,
                ".sql": Language.SQL,
                ".r": Language.R,
                ".scala": Language.SCALA,
            }
            if suffix not in languages:
                raise ValueError(f"Unsupported notebook source extension: {upload.local_path}")
            kwargs.update(format=ImportFormat.SOURCE, language=languages[suffix])
        else:
            kwargs["format"] = ImportFormat.RAW
        ws.workspace.upload(**kwargs)


def _find_job_by_name(ws: Any, name: str) -> str | None:
    for job in ws.jobs.list(name=name):
        if job.job_id is not None:
            return str(job.job_id)
    return None


def deploy(
    job_dir: Path,
    target: str,
    variables: dict[str, str],
    action: str | None = None,
    dry_run: bool = False,
    *,
    ws: Any | None = None,
) -> int:
    """Create/reset or destroy one job sub-instance."""
    from databricks.sdk.service import jobs as jobs_sdk

    app_instance = job_dir.parent.parent
    instance_name = app_instance.name
    sub_instance = job_dir.name
    client = ws

    state = load_state(
        client,
        instance_name,
        "jobs",
        sub_instance,
        target,
    )
    existing_job_id = str(state.get("job_id", ""))

    if action == "destroy":
        if not existing_job_id:
            print(f"No job_id in state for {sub_instance}/{target}. Nothing to destroy.")
            return 0
        if dry_run:
            print(f"[DRY RUN] Would delete Databricks Job {existing_job_id}")
            return 0
        if client is None:
            raise RuntimeError("Workspace client is required to delete a job")
        client.jobs.delete(int(existing_job_id))
        delete_state(
            client,
            instance_name,
            "jobs",
            sub_instance,
            target,
        )
        print(f"Deleted Databricks Job {existing_job_id}")
        return 0

    settings_dict, uploads = build_job_settings(job_dir, target, variables)
    if dry_run:
        print(json.dumps(settings_dict, indent=2, sort_keys=True))
        return 0
    if client is None:
        raise RuntimeError("Workspace client is required to deploy a job")

    _upload_sources(client, uploads)
    settings = jobs_sdk.JobSettings.from_dict(settings_dict)

    if not existing_job_id:
        existing_job_id = _find_job_by_name(client, settings_dict["name"]) or ""

    if existing_job_id:
        client.jobs.reset(int(existing_job_id), settings)
        deployed_job_id = existing_job_id
        audit_action = "update"
        print(f"Updated Databricks Job {settings_dict['name']} ({deployed_job_id})")
    else:
        # Pass the full JobSettings surface (clusters, notifications,
        # git_source, continuous, …) — not a hand-curated subset.  Read the
        # attributes rather than ``as_dict()``: the SDK serialises each
        # nested argument itself, so it needs the typed ``Task`` and
        # ``CronSchedule`` objects, and a flattened dict makes ``create``
        # fail with "'dict' object has no attribute 'as_dict'".  Every
        # ``JobSettings`` field is a valid ``create`` argument.
        create_kwargs = {key: value for key, value in vars(settings).items() if value is not None}
        response = client.jobs.create(**create_kwargs)
        if response.job_id is None:
            raise RuntimeError("Jobs API create response did not include job_id")
        deployed_job_id = str(response.job_id)
        audit_action = "create"
        print(f"Created Databricks Job {settings_dict['name']} ({deployed_job_id})")

    save_state(
        client,
        instance_name,
        "jobs",
        sub_instance,
        target,
        {"job_id": deployed_job_id, "job_name": settings_dict["name"]},
        action=audit_action,
        deployer=variables.get("deployer", ""),
        resource_id=deployed_job_id,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deploy a Databricks Job")
    parser.add_argument(
        "--instance",
        required=True,
        help="App name (deploys every job), '<app>/<job>', '<app>/jobs/<job>', or a path to either",
    )
    parser.add_argument("--target", default="dev")
    parser.add_argument("--action", choices=("deploy", "destroy"), default="deploy")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--vars", nargs="*", default=[])
    add_project_root_arg(parser)
    args = parser.parse_args(argv)

    try:
        instance_dir, job_dirs = resolve_sub_instances(args.project_root, args.instance, "jobs")
    except ValueError as exc:
        print(f"ERROR: {exc}")
        return 1
    variables = load_brick_variables(
        args.project_root,
        args.target,
        instance_dir,
        extra_vars=args.vars,
    )
    from agent_foundry.workspace_client import prefer_workspace_client

    ws = prefer_workspace_client(variables)
    variables = fill_state_outputs(variables, instance_dir, args.target, ws)
    failures = 0
    for job_dir in job_dirs:
        try:
            failures += deploy(
                job_dir,
                args.target,
                variables,
                action="destroy" if args.action == "destroy" else None,
                dry_run=args.dry_run,
                ws=ws,
            )
        except (OSError, ValueError, RuntimeError) as exc:
            print(f"ERROR: {exc}")
            failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
