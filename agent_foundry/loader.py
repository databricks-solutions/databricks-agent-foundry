"""Two-file ``foundry.toml`` loader (issue #191, F-PR2).

This module is the disk-reading counterpart to the typed models added
in F-PR1 (``agent_foundry.public.foundry_config``).  Given a repo
root, it composes a runtime :class:`~agent_foundry.config.InstanceConfig`
from:

* ``<repo_root>/foundry.toml`` -- the repo-root file authored against
  :class:`~agent_foundry.public.FoundryRepoConfig`.
* ``<repo_root>/<instance.path>/foundry.toml`` -- one file per
  ``[[instances]]`` entry, authored against
  :class:`~agent_foundry.public.FoundryInstanceConfig`.
* Per-brick YAML files (``warehouse/config.yml``,
  ``evaluation/eval_config.yml``), read via the helpers in
  :mod:`agent_foundry.config`.

Payload concerns (the agent ``spec`` in ``payloads/agent/payload.yml``)
are deliberately NOT read here -- the framework loader stays
payload-agnostic; the payload reads its own ``spec``.

The output is the runtime :class:`InstanceConfig` the rest of the
framework (bricks, orchestrator, CLI verbs) consumes.

A missing repo-root ``foundry.toml`` raises :class:`ConfigNotFoundError`;
a declared instance without its per-instance file raises
:class:`PartialMigrationError`.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from agent_foundry.config import (
    EvaluationConfig,
    InstanceConfig,
    TargetConfig,
    WarehouseConfig,
    _load_builtin_eval_defaults,
    extract_evaluation_config,
    extract_warehouse_config,
    load_yaml,
)
from agent_foundry.public.foundry_config import (
    CURRENT_SCHEMA_VERSION,
    FoundryInstanceConfig,
    FoundryRepoConfig,
    InstanceRef,
)

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class FoundryConfigError(Exception):
    """Base class for ``foundry.toml`` loader errors.

    Catch this to handle any loader failure uniformly; catch the
    concrete subclasses below when you need to distinguish "config
    missing" from "partial migration".
    """


class ConfigNotFoundError(FoundryConfigError):
    """No repo-root ``foundry.toml`` found at ``repo_root``."""


class PartialMigrationError(FoundryConfigError):
    """Repo-root ``foundry.toml`` declares an instance whose per-instance
    ``foundry.toml`` is missing.
    """


# ---------------------------------------------------------------------------
# Public loaders
# ---------------------------------------------------------------------------


REPO_FILE_NAME = "foundry.toml"
INSTANCE_FILE_NAME = "foundry.toml"


def load_repo_config(repo_root: Path) -> FoundryRepoConfig:
    """Read and validate the repo-root ``foundry.toml``.

    Does not touch per-instance files; that is :func:`load_instance_config`'s
    job.  Splitting the two reads makes ``agent-foundry config show``
    cheaper for repo-level introspection and gives F-PR4 a small
    surface to call.
    """
    repo_root = repo_root.resolve()
    path = repo_root / REPO_FILE_NAME
    if not path.exists():
        raise ConfigNotFoundError(
            f"No {REPO_FILE_NAME} at {repo_root}. Author a foundry.toml at the repo root (see issue #191)."
        )
    with open(path, "rb") as f:
        data = tomllib.load(f)
    # Check ``schema_version`` *before* full model validation so older
    # repos get a structured migration error pointing at the CHANGELOG
    # instead of a generic Pydantic "extra inputs" failure on the
    # repo-shape fields that changed between versions.
    declared_version = data.get("schema_version", CURRENT_SCHEMA_VERSION)
    if declared_version != CURRENT_SCHEMA_VERSION:
        raise FoundryConfigError(
            f"{path}: schema_version = {declared_version} is not "
            f"supported. This framework version requires schema_version "
            f"= {CURRENT_SCHEMA_VERSION}. Migration guide: see the "
            f"agent-foundry CHANGELOG entry for the 1 -> 2 bump "
            f"(warehouse ownership moved from [targets.<name>] to "
            f"per-instance [warehouse])."
        )
    return FoundryRepoConfig.model_validate(data)


def load_instance_config(
    repo_root: Path,
    *,
    instance: str | None = None,
    target_name: str = "dev",
    extra_vars: list[str] | None = None,
) -> InstanceConfig:
    """Compose a runtime :class:`InstanceConfig` from ``foundry.toml`` files.

    ``instance`` selects which ``[[instances]]`` entry to compose.  If
    omitted, the call succeeds only when the repo declares exactly one
    instance -- matching the ergonomic shortcut F-PR5 will add to the
    ``agent-foundry deploy`` CLI.

    ``extra_vars`` accepts the CLI's ``--vars`` ``key=value`` overrides.
    """
    repo_root = repo_root.resolve()
    repo = load_repo_config(repo_root)

    instance_ref = _select_instance(repo, instance=instance)
    instance_dir = (repo_root / instance_ref.path).resolve()
    inst_path = instance_dir / INSTANCE_FILE_NAME
    instance_cfg = read_instance_file(instance_dir)
    if instance_cfg is None:
        raise PartialMigrationError(
            f"Repo-root {REPO_FILE_NAME} declares instance at "
            f"{instance_ref.path!s} but no {INSTANCE_FILE_NAME} exists "
            f"at {inst_path}. Author the per-instance foundry.toml or "
            f"remove the [[instances]] entry."
        )

    # Strict-overlap rule: a "reuse" warehouse mode means the instance
    # binds to an existing warehouse; the warehouse brick is a no-op
    # and a stale warehouse/config.yml would silently misconfigure
    # downstream readers. Reject it explicitly so authors fix one
    # source of truth.
    if instance_cfg.warehouse.mode == "reuse":
        wh_yaml = instance_dir / "warehouse" / "config.yml"
        if wh_yaml.exists():
            raise FoundryConfigError(
                f'{inst_path}: [warehouse] mode = "reuse" but '
                f"{wh_yaml} exists. In reuse mode the warehouse brick "
                f"does not provision a warehouse, so warehouse/config.yml "
                f"(size/auto_stop/tags) is dead config. Delete "
                f'{wh_yaml} or switch to mode = "dedicated".'
            )

    target = repo.targets.get(target_name)
    if target is None:
        available = ", ".join(sorted(repo.targets)) or "(none)"
        raise FoundryConfigError(
            f"Target {target_name!r} not declared in {REPO_FILE_NAME}. Available targets: {available}."
        )

    # CLI overrides apply to the merged variable bag.  Identity fields
    # cannot be overridden via --vars: they're authored, not flagged.
    cli_overrides = _parse_extra_vars(extra_vars)

    return _compose_instance_config(
        repo_root=repo_root,
        instance_dir=instance_dir,
        target_name=target_name,
        target=target,
        instance_cfg=instance_cfg,
        cli_overrides=cli_overrides,
    )


def read_instance_file(instance_dir: Path) -> FoundryInstanceConfig | None:
    """Read and validate ``<instance_dir>/foundry.toml``, or ``None`` if absent.

    Needs no repo-root file, so it also works inside a deployed App where
    only the instance directory is staged.
    """
    path = instance_dir / INSTANCE_FILE_NAME
    if not path.is_file():
        return None
    with open(path, "rb") as f:
        return FoundryInstanceConfig.model_validate(tomllib.load(f))


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _select_instance(
    repo: FoundryRepoConfig,
    *,
    instance: str | None,
) -> InstanceRef:
    if not repo.instances:
        raise FoundryConfigError(f"{REPO_FILE_NAME} declares no [[instances]] entries.")
    if instance is None:
        if len(repo.instances) == 1:
            return repo.instances[0]
        names = ", ".join(str(i.path) for i in repo.instances)
        raise FoundryConfigError(
            f"{REPO_FILE_NAME} declares multiple instances ({names}); pass --instance to disambiguate."
        )
    # Match by directory basename: that's the user-facing instance name.
    for ref in repo.instances:
        if ref.path.name == instance:
            return ref
    available = ", ".join(ref.path.name for ref in repo.instances)
    raise FoundryConfigError(f"Instance {instance!r} not found in {REPO_FILE_NAME}. Available instances: {available}.")


def _parse_extra_vars(extra_vars: list[str] | None) -> dict[str, str]:
    """Parse ``key=value`` strings from ``--vars`` into a flat dict."""
    out: dict[str, str] = {}
    for kv in extra_vars or []:
        if "=" not in kv:
            continue
        k, v = kv.split("=", 1)
        out[k] = v
    return out


def _compose_instance_config(
    *,
    repo_root: Path,
    instance_dir: Path,
    target_name: str,
    target: object,  # public.Target; typed loosely to avoid circular imports.
    instance_cfg: FoundryInstanceConfig,
    cli_overrides: dict[str, str],
) -> InstanceConfig:
    """Merge the typed authoring models with per-brick config into the
    runtime :class:`InstanceConfig` the rest of the framework consumes.

    Payload concerns (agent runtime config, UI toggles, integrations)
    are NOT composed here -- they live in the payload's ``spec`` and are
    read by the payload itself.  This stays agent-agnostic: only
    identity / target / warehouse / evaluation.
    """
    wh_raw = load_yaml(instance_dir / "warehouse" / "config.yml")
    wh_kwargs = extract_warehouse_config(wh_raw)

    eval_defaults = _load_builtin_eval_defaults()
    eval_instance = load_yaml(instance_dir / "evaluation" / "eval_config.yml")
    eval_kwargs = extract_evaluation_config(eval_defaults, eval_instance)

    # CLI overrides apply only to the target layer.  Identity fields
    # come from the per-instance foundry.toml and cannot be flagged.
    target_host = cli_overrides.get("host", target.host)  # type: ignore[attr-defined]
    target_catalog = cli_overrides.get("catalog", target.catalog)  # type: ignore[attr-defined]
    target_schema = cli_overrides.get("schema", target.schema_)  # type: ignore[attr-defined]
    target_profile = cli_overrides.get("profile", target.profile)  # type: ignore[attr-defined]
    target_deployer = cli_overrides.get("deployer", target.deployer)  # type: ignore[attr-defined]

    # Warehouse binding:
    #   * mode = "reuse"     -> seed runtime TargetConfig.warehouse_id from
    #                            the authored value so downstream bricks
    #                            see it immediately (no warehouse-brick
    #                            deploy will run).
    #   * mode = "dedicated" -> leave empty; the warehouse brick will
    #                            populate it from its _state file once
    #                            the warehouse has been provisioned.
    # ``--vars warehouse_id=...`` still wins so that operators can pin
    # a one-off override without editing files.
    if instance_cfg.warehouse.mode == "reuse":
        default_warehouse_id = instance_cfg.warehouse.warehouse_id
    else:
        default_warehouse_id = ""
    target_warehouse_id = cli_overrides.get("warehouse_id", default_warehouse_id)

    config = InstanceConfig(
        name=instance_cfg.name,
        description=instance_cfg.description,
        target_name=target_name,
        schema_prefix=instance_cfg.schema_prefix,
        vs_endpoint_name=instance_cfg.vs_endpoint_name,
        app_name=instance_cfg.app_name,
        chat_ui_app_name=instance_cfg.chat_ui_app_name,
        target=TargetConfig(
            host=str(target_host),
            catalog=str(target_catalog),
            schema_=str(target_schema),
            warehouse_id=str(target_warehouse_id),
            profile=str(target_profile),
            deployer=str(target_deployer),
        ),
        warehouse=WarehouseConfig(**wh_kwargs),
        evaluation=EvaluationConfig(**eval_kwargs),
    )
    # Lazy: payload_adapters imports this module.
    from agent_foundry.deploy.payload_adapters import payload_app_resource_name

    resource_name = payload_app_resource_name(instance_dir, config.app_resource_name)
    if resource_name == config.app_resource_name:
        return config
    return config.model_copy(update={"app_resource_name_override": resource_name})


__all__ = [
    "ConfigNotFoundError",
    "FoundryConfigError",
    "INSTANCE_FILE_NAME",
    "PartialMigrationError",
    "REPO_FILE_NAME",
    "load_instance_config",
    "load_repo_config",
    "read_instance_file",
]
