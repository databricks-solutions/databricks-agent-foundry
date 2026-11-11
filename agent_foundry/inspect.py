"""Rendering helpers for ``agent-foundry config show`` (issue #191, F-PR4).

Why a separate module
---------------------

``agent_foundry/deploy/cli/`` is the Typer wrapper layer; pure
business logic belongs in a library module so it can be
unit-tested without the CliRunner.  This module
contains the two view-builder functions called by the CLI:

* :func:`build_authored_view` -- the **on-disk shape** view.  Merges
  the chosen target block from the repo-root ``foundry.toml`` with
  the per-instance ``foundry.toml``.  Round-trippable back to TOML.
* :func:`build_resolved_view` -- the **runtime shape** view.  Returns
  the :class:`InstanceConfig` the orchestrator actually sees,
  serialised to a JSON-compatible dict.  Includes computed fields
  (e.g. the merged schema name) that aren't part of the authored
  contract.

Both views can optionally include data-layer YAMLs (golden datasets,
view definitions, etc.) read from the instance directory with
``${var.*}`` substitutions applied.  That's gated by the
``include_data`` flag so the default output stays config-focused.

The output is rendered to one of three formats (``toml``, ``json``,
``yaml``) by :func:`render`.  Format choice is independent of view
choice -- both views are plain dicts of JSON-compatible scalars.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any, Literal

import yaml

from agent_foundry.config import InstanceConfig
from agent_foundry.loader import (
    INSTANCE_FILE_NAME,
    REPO_FILE_NAME,
    FoundryConfigError,
    PartialMigrationError,
    _parse_extra_vars,
    _select_instance,
    load_repo_config,
)
from agent_foundry.public.foundry_config import FoundryInstanceConfig
from agent_foundry.variables import substitute_obj

Format = Literal["toml", "json", "yaml"]

# Filenames that the typed brick contracts already own.  We skip
# these when globbing for "data-layer" YAMLs so we don't double-
# render the typed config under a ``data:`` heading.
_TYPED_BRICK_YAML_BASENAMES = frozenset(
    {
        "config.yml",
        "config.yaml",
        "eval_config.yml",
        "eval_config.yaml",
        "foundry.toml",
    }
)


# ---------------------------------------------------------------------------
# View builders
# ---------------------------------------------------------------------------


def build_authored_view(
    repo_root: Path,
    *,
    instance: str | None,
    target_name: str,
    extra_vars: list[str] | None = None,
    include_data: bool = False,
) -> dict[str, Any]:
    """Return the **on-disk** view: merged repo-root + per-instance TOML.

    Round-trippable back to TOML.  Identity fields appear under
    ``instance``, the resolved environment block under ``target``,
    and -- when ``include_data`` is true -- data-layer YAMLs under
    ``data`` keyed by their relative path inside the instance dir.

    ``extra_vars`` only affects the variable bag used for data-layer
    substitution; identity fields are never overridden via ``--var``
    (that's a deliberate constraint -- they're authored, not flagged).
    """
    repo_root = repo_root.resolve()
    repo = load_repo_config(repo_root)
    instance_ref = _select_instance(repo, instance=instance)

    target = repo.targets.get(target_name)
    if target is None:
        available = ", ".join(sorted(repo.targets)) or "(none)"
        raise FoundryConfigError(
            f"Target {target_name!r} not declared in {REPO_FILE_NAME}. Available targets: {available}."
        )

    instance_dir = (repo_root / instance_ref.path).resolve()
    instance_cfg = _read_instance_toml(instance_dir)

    out: dict[str, Any] = {
        "schema_version": repo.schema_version,
        "target": {
            "name": target_name,
            **_dump_pydantic(target),
        },
        "instance": {
            "path": str(instance_ref.path),
            **_dump_pydantic(instance_cfg),
        },
    }

    if include_data:
        variables = _build_var_bag(target, instance_cfg, extra_vars)
        out["data"] = _collect_data_yamls(instance_dir, variables)

    return out


def build_resolved_view(
    repo_root: Path,
    *,
    instance: str | None,
    target_name: str,
    extra_vars: list[str] | None = None,
    include_data: bool = False,
) -> dict[str, Any]:
    """Return the **runtime** view: the composed :class:`InstanceConfig`.

    This is what the orchestrator sees at deploy time -- including
    framework defaults applied on top of the authored YAML/TOML and
    computed fields like the post-merge schema name.  Use this view
    to debug deploys that misbehave despite the authored config
    looking right.
    """
    # Local import avoids a circular import via load_instance_config ->
    # this module's imports above.
    from agent_foundry.loader import load_instance_config

    cfg: InstanceConfig = load_instance_config(
        repo_root,
        instance=instance,
        target_name=target_name,
        extra_vars=extra_vars,
    )

    out: dict[str, Any] = _instance_config_to_dict(cfg)

    if include_data:
        # Reuse the authored-view path to discover the instance dir
        # (cheap: we already validated the repo above).
        repo = load_repo_config(repo_root)
        instance_ref = _select_instance(repo, instance=instance)
        instance_dir = (repo_root / instance_ref.path).resolve()
        # ``InstanceConfig.to_variables()`` is the canonical way to
        # derive the substitution bag the bricks see at deploy time.
        # CLI overrides are already folded in by the loader call above.
        variables = cfg.to_variables()
        out["data"] = _collect_data_yamls(instance_dir, variables)

    return out


# ---------------------------------------------------------------------------
# Format rendering
# ---------------------------------------------------------------------------


def render(view: dict[str, Any], fmt: Format) -> str:
    """Serialise *view* to the chosen format.

    * ``toml`` is the default in the CLI; matches the authoring format
      so output is paste-back-able into ``foundry.toml`` for the
      authored view.
    * ``json`` is the friendliest format for piping into ``jq`` /
      automation.
    * ``yaml`` is offered for parity with the legacy data-layer
      YAML files; identical semantics to ``json`` since both views
      are JSON-compatible scalars.
    """
    if fmt == "json":
        return json.dumps(view, indent=2, sort_keys=True, default=str) + "\n"
    if fmt == "yaml":
        return yaml.safe_dump(view, sort_keys=True, default_flow_style=False)
    if fmt == "toml":
        return _to_toml(view)
    # The CLI restricts fmt to the Literal above via Typer; this branch
    # only fires if a programmatic caller passes a bad value.
    raise ValueError(f"Unknown format: {fmt!r}")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _read_instance_toml(instance_dir: Path) -> FoundryInstanceConfig:
    inst_path = instance_dir / INSTANCE_FILE_NAME
    if not inst_path.exists():
        raise PartialMigrationError(f"Per-instance {INSTANCE_FILE_NAME} missing at {inst_path}.")
    with open(inst_path, "rb") as f:
        data = tomllib.load(f)
    return FoundryInstanceConfig.model_validate(data)


def _dump_pydantic(model: Any) -> dict[str, Any]:
    """Pydantic ``model_dump`` with aliases enabled.

    Uses ``by_alias=True`` so the on-disk authoring name (``schema``,
    not ``schema_``) shows up in the output.  Mirrors what the YAML
    LS sees via the JSON Schema bundle.
    """
    return model.model_dump(by_alias=True, mode="json")


def _instance_config_to_dict(cfg: InstanceConfig) -> dict[str, Any]:
    """Serialise the runtime InstanceConfig to a JSON-compatible dict.

    ``InstanceConfig`` is a Pydantic model; ``model_dump(mode="json")``
    walks nested models and coerces non-JSON types (Path, datetime)
    to strings.  Aliases stay un-applied here because the runtime
    view uses the internal Python names (``schema_``, not ``schema``);
    the authored view is where alias projection matters.
    """
    return cfg.model_dump(mode="json")


def _build_var_bag(
    target: Any,
    instance_cfg: FoundryInstanceConfig,
    extra_vars: list[str] | None,
) -> dict[str, str]:
    """Build the ``${var.*}`` substitution bag for data-layer YAMLs.

    Mirrors the precedence chain F-PR2's loader uses internally:

    1. ``instance_cfg`` fields surfaced as flat keys (``name``,
       ``schema_prefix``, ``vs_endpoint_name``, ...).
    2. ``target`` fields (``catalog``, ``host``, ...).
    3. ``extra_vars`` from the CLI.

    Identity wins for keys that appear in both -- matching the
    authored intent that target settings are environment-level and
    don't shadow per-instance choices.
    """
    bag: dict[str, str] = {}
    for k, v in _dump_pydantic(target).items():
        if v is not None:
            bag[k] = str(v)
    for k, v in _dump_pydantic(instance_cfg).items():
        if v is None or isinstance(v, (dict, list)):
            continue
        bag[k] = str(v)
    bag.update(_parse_extra_vars(extra_vars) or {})
    return bag


def _collect_data_yamls(
    instance_dir: Path,
    variables: dict[str, str],
) -> dict[str, Any]:
    """Read every non-typed YAML under *instance_dir* with substitutions.

    Skips the typed brick YAMLs (their typed shapes are already
    surfaced in the resolved view) and the new ``foundry.toml``.
    Returns a dict keyed by the relative path so the operator can
    locate the file on disk from the output alone.
    """
    out: dict[str, Any] = {}
    if not instance_dir.exists():
        return out
    for yml in sorted(instance_dir.rglob("*.yml")):
        if yml.name in _TYPED_BRICK_YAML_BASENAMES:
            continue
        rel = yml.relative_to(instance_dir).as_posix()
        with open(yml, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        out[rel] = substitute_obj(raw, variables, strict=False)
    for yml in sorted(instance_dir.rglob("*.yaml")):
        if yml.name in _TYPED_BRICK_YAML_BASENAMES:
            continue
        rel = yml.relative_to(instance_dir).as_posix()
        with open(yml, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        out[rel] = substitute_obj(raw, variables, strict=False)
    return out


# ---------------------------------------------------------------------------
# TOML rendering
# ---------------------------------------------------------------------------


def _to_toml(view: dict[str, Any]) -> str:
    """Render *view* to TOML without taking on a new runtime dependency.

    The stdlib has ``tomllib`` for reading TOML but no writer until
    Python 3.13+.  Rather than pull in ``tomli-w`` or ``tomlkit``, we
    implement a small writer here that handles the JSON-compatible
    scalar types our views produce (str, int, float, bool, list,
    dict, None).

    This is intentionally minimal -- complex shapes (lists of mixed
    types, multiline strings with embedded quotes) are out of scope.
    The views we generate stay within the supported subset.
    """
    lines: list[str] = []
    _emit_table(view, prefix=(), lines=lines)
    return "\n".join(lines).rstrip() + "\n"


def _emit_table(
    table: dict[str, Any],
    *,
    prefix: tuple[str, ...],
    lines: list[str],
) -> None:
    # First pass: scalar keys and arrays go directly in the current
    # section.  Nested dicts are emitted afterwards under their own
    # ``[prefix.key]`` header so the output stays valid TOML.
    scalars: list[tuple[str, Any]] = []
    nested: list[tuple[str, dict[str, Any]]] = []
    for key, value in table.items():
        if isinstance(value, dict):
            nested.append((key, value))
        else:
            scalars.append((key, value))

    if scalars or not prefix:
        if prefix:
            lines.append(f"[{'.'.join(prefix)}]")
        for key, value in scalars:
            lines.append(f"{key} = {_format_value(value)}")
        if scalars:
            lines.append("")

    for key, value in nested:
        _emit_table(value, prefix=(*prefix, key), lines=lines)


def _format_value(value: Any) -> str:
    if value is None:
        # TOML has no null; emit empty string as the least-surprising
        # placeholder.  The authored config never contains None for
        # required fields (Pydantic validation enforces that upstream).
        return '""'
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(value, list):
        return "[" + ", ".join(_format_value(v) for v in value) + "]"
    # Fall back to JSON-style string for anything exotic.
    return json.dumps(value, default=str)
