"""Deploy serverless Model Serving endpoints declared by ``model_serving/<name>/endpoint_config.yml``."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from agent_foundry.bricks import fill_state_outputs, load_brick_variables, resolve_sub_instances
from agent_foundry.config import merge_managed_by_tags
from agent_foundry.contracts.model_serving import ModelBuildConfig, ServingEndpointConfig
from agent_foundry.loader import FoundryConfigError
from agent_foundry.state import delete_state, load_state, save_state
from agent_foundry.variables import add_project_root_arg, substitute_obj

CONFIG_FILENAME = "endpoint_config.yml"
STATE_TYPE = "model_serving"
SERVED_ENTITY_NAME = "model"
ENDPOINT_TIMEOUT = timedelta(minutes=60)
BUILD_LOG_TAIL_LINES = 40


def load_endpoint_config(sub_dir: Path, variables: dict[str, str]) -> ServingEndpointConfig:
    """Parse and validate ``endpoint_config.yml`` with ``${var.*}`` substituted."""
    path = sub_dir / CONFIG_FILENAME
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    try:
        return ServingEndpointConfig.model_validate(substitute_obj(raw, variables))
    except (ValueError, ValidationError) as exc:
        raise FoundryConfigError(f"Invalid {path}: {exc}") from exc


def _local_file(sub_dir: Path, configured: str) -> Path:
    root = sub_dir.resolve()
    candidate = (root / configured[2:]).resolve()
    if root not in candidate.parents:
        raise ValueError(f"Build source escapes {sub_dir}: {configured}")
    if not candidate.is_file():
        raise FileNotFoundError(f"Build source not found: {candidate}")
    return candidate


def _build_sources(sub_dir: Path, build: ModelBuildConfig) -> list[Path]:
    return [_local_file(sub_dir, path) for path in (build.script, *build.files)]


def build_hash(sub_dir: Path, config: ServingEndpointConfig) -> str:
    """Digest of everything a build depends on; a change triggers a rebuild."""
    assert config.build is not None
    digest = hashlib.sha256()
    digest.update(json.dumps([config.model.name, sorted(config.build.dependencies)]).encode())
    for source in _build_sources(sub_dir, config.build):
        digest.update(source.relative_to(sub_dir.resolve()).as_posix().encode())
        digest.update(source.read_bytes())
    return digest.hexdigest()


def workspace_root(target: str, app_name: str, sub_instance: str) -> str:
    for label, value in (("target", target), ("app_name", app_name), ("sub-instance", sub_instance)):
        if not value or value in {".", ".."} or "/" in value or "\\" in value:
            raise ValueError(f"Invalid {label}: {value!r}")
    return f"/Shared/agent-foundry/{target}/{app_name}/model_serving/{sub_instance}"


def _upload_sources(ws: Any, sub_dir: Path, sources: list[Path], root: str) -> None:
    from databricks.sdk.service.workspace import ImportFormat

    for source in sources:
        remote = f"{root}/{source.relative_to(sub_dir.resolve()).as_posix()}"
        ws.workspace.mkdirs(remote.rsplit("/", 1)[0])
        ws.workspace.upload(
            path=remote, content=io.BytesIO(source.read_bytes()), format=ImportFormat.RAW, overwrite=True
        )


def _lookup(fetch: Any, **kwargs: Any) -> Any | None:
    from databricks.sdk.errors import NotFound

    try:
        return fetch(**kwargs)
    except NotFound:
        return None


def latest_version(ws: Any, model_name: str) -> str | None:
    versions = [info.version for info in ws.model_versions.list(model_name) if info.version is not None]
    return str(max(versions)) if versions else None


def resolve_version(ws: Any, config: ServingEndpointConfig) -> str:
    """The version ``model.version`` / ``model.alias`` / latest selects."""
    model = config.model
    if model.version:
        return model.version
    if model.alias:
        return str(ws.model_versions.get_by_alias(model.name, model.alias).version)
    version = latest_version(ws, model.name)
    if version is None:
        raise RuntimeError(f"Model {model.name} has no registered version")
    return version


def _run_build(ws: Any, sub_dir: Path, config: ServingEndpointConfig, root: str) -> str:
    """Upload the build sources, run them as a serverless job, return the new version."""
    from databricks.sdk.service import compute, jobs

    build = config.build
    assert build is not None
    _upload_sources(ws, sub_dir, _build_sources(sub_dir, build), root)
    before = (
        latest_version(ws, config.model.name)
        if _lookup(ws.registered_models.get, full_name=config.model.name)
        else None
    )
    print(f"  Building {config.model.name} (serverless job, up to {build.timeout_minutes} min) ...")
    ws.jobs.submit(
        run_name=f"build {config.model.name}",
        tasks=[
            jobs.SubmitTask(
                task_key="build",
                environment_key="build",
                spark_python_task=jobs.SparkPythonTask(
                    python_file=f"/Workspace{root}/{build.script[2:]}",
                    parameters=["--model-name", config.model.name, "--experiment", f"{root}/build_experiment"],
                ),
            )
        ],
        environments=[
            jobs.JobEnvironment(
                environment_key="build",
                spec=compute.Environment(
                    environment_version=build.environment_version, dependencies=build.dependencies
                ),
            )
        ],
        timeout_seconds=build.timeout_minutes * 60,
    ).result(timeout=timedelta(minutes=build.timeout_minutes + 5))
    after = latest_version(ws, config.model.name)
    if after is None or after == before:
        raise RuntimeError(f"Build finished without registering a new version of {config.model.name}")
    print(f"  Registered {config.model.name} version {after}")
    return after


def _experiment_id(ws: Any, path: str) -> str:
    response = _lookup(ws.experiments.get_by_name, experiment_name=path)
    experiment = getattr(response, "experiment", None)
    return str(getattr(experiment, "experiment_id", "") or "")


def _built_version(
    ws: Any,
    sub_dir: Path,
    config: ServingEndpointConfig,
    state: dict[str, Any],
    root: str,
) -> tuple[str, dict[str, str]]:
    """Reuse the recorded build when its inputs and version are unchanged, else rebuild."""
    digest = build_hash(sub_dir, config)
    recorded = str(state.get("model_version", ""))
    reusable = state.get("build_hash") == digest and recorded
    if reusable and _lookup(ws.model_versions.get, full_name=config.model.name, version=int(recorded)) is not None:
        print(f"  Build inputs unchanged; serving {config.model.name} version {recorded}")
        return recorded, {"build_hash": digest, "experiment_id": str(state.get("experiment_id", ""))}
    version = _run_build(ws, sub_dir, config, root)
    return version, {"build_hash": digest, "experiment_id": _experiment_id(ws, f"{root}/build_experiment")}


def served_entity(config: ServingEndpointConfig, version: str) -> Any:
    from databricks.sdk.service.serving import ServedEntityInput, ServingModelWorkloadType

    model = config.model
    return ServedEntityInput(
        name=SERVED_ENTITY_NAME,
        entity_name=model.name,
        entity_version=version,
        workload_type=ServingModelWorkloadType(model.workload_type),
        workload_size=model.workload_size,
        scale_to_zero_enabled=model.scale_to_zero,
        environment_vars=model.environment_vars or None,
    )


def _entity_matches(current: Any, desired: Any) -> bool:
    fields = ("entity_name", "entity_version", "workload_size", "scale_to_zero_enabled")
    if any(getattr(current, name, None) != getattr(desired, name) for name in fields):
        return False
    if getattr(getattr(current, "workload_type", None), "value", None) != desired.workload_type.value:
        return False
    return (getattr(current, "environment_vars", None) or {}) == (desired.environment_vars or {})


def _is_current(endpoint: Any, desired: Any) -> bool:
    entities = getattr(getattr(endpoint, "config", None), "served_entities", None) or []
    return len(entities) == 1 and _entity_matches(entities[0], desired)


def _print_build_logs(ws: Any, endpoint_name: str) -> None:
    try:
        logs = ws.serving_endpoints.build_logs(endpoint_name, SERVED_ENTITY_NAME).logs or ""
    except Exception as exc:
        print(f"  (no build logs: {exc})")
        return
    for line in logs.splitlines()[-BUILD_LOG_TAIL_LINES:]:
        print(f"    {line}")


def _ensure_endpoint(ws: Any, config: ServingEndpointConfig, desired: Any, tags: dict[str, str]) -> str:
    """Create or update the endpoint; return the audit action."""
    from databricks.sdk.service.serving import EndpointCoreConfigInput, EndpointTag

    name = config.endpoint_name
    existing = _lookup(ws.serving_endpoints.get, name=name)
    if existing is None:
        print(f"  Creating serving endpoint {name} (waits up to {ENDPOINT_TIMEOUT}) ...")
        ws.serving_endpoints.create_and_wait(
            name=name,
            config=EndpointCoreConfigInput(name=name, served_entities=[desired]),
            tags=[EndpointTag(key=key, value=value) for key, value in tags.items()],
            timeout=ENDPOINT_TIMEOUT,
        )
        return "create"
    ws.serving_endpoints.wait_get_serving_endpoint_not_updating(name, timeout=ENDPOINT_TIMEOUT)
    if _is_current(existing, desired):
        print(f"  Serving endpoint {name} already serves version {desired.entity_version}")
        return "update"
    print(f"  Updating serving endpoint {name} to version {desired.entity_version} ...")
    ws.serving_endpoints.update_config_and_wait(name=name, served_entities=[desired], timeout=ENDPOINT_TIMEOUT)
    return "update"


def _destroy(ws: Any, state: dict[str, Any]) -> None:
    from databricks.sdk.errors import NotFound

    endpoint_name = str(state["endpoint_name"])
    try:
        ws.serving_endpoints.delete(endpoint_name)
        print(f"Deleted serving endpoint {endpoint_name}")
    except NotFound:
        print(f"Serving endpoint {endpoint_name} already gone")
    if not state.get("build_hash"):
        return
    model_name = str(state["model_name"])
    try:
        ws.registered_models.delete(model_name)
        print(f"Deleted built model {model_name}")
    except NotFound:
        print(f"Built model {model_name} already gone")
    if state.get("experiment_id"):
        ws.experiments.delete_experiment(str(state["experiment_id"]))
        print(f"Deleted build experiment {state['experiment_id']}")


def _print_plan(config: ServingEndpointConfig) -> None:
    plan = config.model_dump(exclude_none=True)
    print(f"[DRY RUN] Would deploy serving endpoint:\n{json.dumps(plan, indent=2, sort_keys=True)}")


def deploy(
    sub_dir: Path,
    target: str,
    variables: dict[str, str],
    action: str | None = None,
    dry_run: bool = False,
    *,
    ws: Any | None = None,
) -> int:
    """Create/update or destroy one endpoint sub-instance."""
    instance_name = sub_dir.parent.parent.name
    sub_instance = sub_dir.name
    state = load_state(ws, instance_name, STATE_TYPE, sub_instance, target)

    if action == "destroy":
        if not state.get("endpoint_name"):
            print(f"No endpoint in state for {sub_instance}/{target}. Nothing to destroy.")
            return 0
        if dry_run:
            print(f"[DRY RUN] Would delete serving endpoint {state['endpoint_name']}")
            return 0
        if ws is None:
            raise RuntimeError("Workspace client is required to delete a serving endpoint")
        _destroy(ws, state)
        delete_state(ws, instance_name, STATE_TYPE, sub_instance, target)
        return 0

    config = load_endpoint_config(sub_dir, variables)
    if dry_run:
        _print_plan(config)
        return 0
    if ws is None:
        raise RuntimeError("Workspace client is required to deploy a serving endpoint")

    root = workspace_root(target, variables.get("app_name", instance_name), sub_instance)
    build_state: dict[str, str] = {}
    if config.build:
        version, build_state = _built_version(ws, sub_dir, config, state, root)
    else:
        version = resolve_version(ws, config)
    desired = served_entity(config, version)
    try:
        audit_action = _ensure_endpoint(
            ws, config, desired, merge_managed_by_tags(variables.get("app_name"), config.tags)
        )
    except Exception:
        print(f"ERROR: serving endpoint {config.endpoint_name} did not become ready. Last build log lines:")
        _print_build_logs(ws, config.endpoint_name)
        raise

    save_state(
        ws,
        instance_name,
        STATE_TYPE,
        sub_instance,
        target,
        {
            "endpoint_name": config.endpoint_name,
            "model_name": config.model.name,
            "model_version": version,
            **build_state,
        },
        action=audit_action,
        deployer=variables.get("deployer", ""),
        resource_id=config.endpoint_name,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deploy a serverless Model Serving endpoint")
    parser.add_argument(
        "--instance",
        required=True,
        help="App name (deploys every endpoint), '<app>/<name>', '<app>/model_serving/<name>', or a path to either",
    )
    parser.add_argument("--target", default="dev")
    parser.add_argument("--action", choices=("deploy", "destroy"), default="deploy")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--vars", nargs="*", default=[])
    add_project_root_arg(parser)
    args = parser.parse_args(argv)

    try:
        instance_dir, sub_dirs = resolve_sub_instances(args.project_root, args.instance, STATE_TYPE)
    except ValueError as exc:
        print(f"ERROR: {exc}")
        return 1
    variables = load_brick_variables(args.project_root, args.target, instance_dir, extra_vars=args.vars)
    from agent_foundry.workspace_client import prefer_workspace_client

    ws = prefer_workspace_client(variables)
    variables = fill_state_outputs(variables, instance_dir, args.target, ws)
    failures = 0
    for sub_dir in sub_dirs:
        try:
            failures += deploy(
                sub_dir,
                args.target,
                variables,
                action="destroy" if args.action == "destroy" else None,
                dry_run=args.dry_run,
                ws=ws,
            )
        except (OSError, ValueError, RuntimeError, FoundryConfigError) as exc:
            print(f"ERROR: {exc}")
            failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
