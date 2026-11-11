"""Tests for ``agent_foundry.inspect`` -- the rendering library
backing ``agent-foundry config show`` (issue #191, F-PR4).

Why test the library layer separately from the CLI

The CLI wrapper (in agent_foundry/tests/unit/test_cli_config_show.py) exercises Typer
plumbing, exit codes, and argument parsing.  This file exercises
the view builders and the format renderer in isolation so failures
point at the right layer.  Splitting them keeps each suite fast
and the assertions narrow.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from agent_foundry.inspect import (
    build_authored_view,
    build_resolved_view,
    render,
)
from agent_foundry.loader import FoundryConfigError

# ---------------------------------------------------------------------------
# Fixtures: minimal two-file foundry.toml repo
# ---------------------------------------------------------------------------


def _write_repo(
    root: Path,
    *,
    instance_name: str = "demo",
    instance_path: str = "instances/demo",
    schema_prefix: str = "demo_app",
    extra_repo_toml: str = "",
    extra_instance_toml: str = "",
    data_files: dict[str, str] | None = None,
) -> Path:
    """Materialise a synthetic two-file foundry.toml layout.

    ``extra_repo_toml`` / ``extra_instance_toml`` let individual
    tests append target/instance specifics without duplicating the
    boilerplate.
    """
    repo_toml = (
        textwrap.dedent(
            f"""
        schema_version = 2

        [targets.dev]
        host = "https://dev.cloud.databricks.com"
        catalog = "main"
        profile = "dev-profile"

        [[instances]]
        path = "{instance_path}"
        """
        ).lstrip()
        + extra_repo_toml
    )
    (root / "foundry.toml").write_text(repo_toml, encoding="utf-8")

    instance_dir = root / instance_path
    instance_dir.mkdir(parents=True, exist_ok=True)
    instance_toml = (
        textwrap.dedent(
            f"""
        name = "{instance_name}"
        description = "test instance"
        schema_prefix = "{schema_prefix}"
        vs_endpoint_name = "vs-demo"
        app_name = "demo-app"
        """
        ).lstrip()
        + extra_instance_toml
    )
    (instance_dir / "foundry.toml").write_text(instance_toml, encoding="utf-8")

    for rel, body in (data_files or {}).items():
        target = instance_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")

    return instance_dir


# ---------------------------------------------------------------------------
# build_authored_view
# ---------------------------------------------------------------------------


def test_authored_view_merges_target_and_instance(tmp_path: Path) -> None:
    _write_repo(tmp_path)
    view = build_authored_view(tmp_path, instance=None, target_name="dev")

    assert view["schema_version"] == 2
    assert view["target"] == {
        "name": "dev",
        "catalog": "main",
        "deployer": "",
        "host": "https://dev.cloud.databricks.com",
        "profile": "dev-profile",
        # The on-disk authoring name is ``schema`` (alias), not
        # ``schema_``.  The view honours that by using by_alias=True.
        "schema": "analytics",
    }
    assert view["instance"]["name"] == "demo"
    assert view["instance"]["schema_prefix"] == "demo_app"
    assert view["instance"]["path"] == "instances/demo"


def test_authored_view_omits_data_by_default(tmp_path: Path) -> None:
    """``include_data`` defaults to False; data-layer YAMLs are not
    in the output unless the operator opts in.
    """
    _write_repo(
        tmp_path,
        data_files={"data/golden.yml": "rows:\n  - id: 1\n"},
    )
    view = build_authored_view(tmp_path, instance=None, target_name="dev")
    assert "data" not in view


def test_authored_view_includes_data_when_requested(tmp_path: Path) -> None:
    _write_repo(
        tmp_path,
        data_files={
            "data/golden.yml": "rows:\n  - id: 1\n  - id: 2\n",
        },
    )
    view = build_authored_view(tmp_path, instance=None, target_name="dev", include_data=True)
    assert view["data"] == {"data/golden.yml": {"rows": [{"id": 1}, {"id": 2}]}}


def test_authored_view_substitutes_vars_in_data_yamls(tmp_path: Path) -> None:
    """Data-layer YAMLs see the merged variable bag: target fields
    (``catalog``), instance fields (``schema_prefix``), and CLI
    overrides (``--var``).
    """
    _write_repo(
        tmp_path,
        data_files={
            "data/view.sql.yml": (
                "name: ${var.schema_prefix}_view\ncatalog: ${var.catalog}\noverride: ${var.cli_only}\n"
            ),
        },
    )
    view = build_authored_view(
        tmp_path,
        instance=None,
        target_name="dev",
        include_data=True,
        extra_vars=["cli_only=hello"],
    )
    assert view["data"]["data/view.sql.yml"] == {
        "name": "demo_app_view",
        "catalog": "main",
        "override": "hello",
    }


def test_authored_view_skips_typed_brick_yamls(tmp_path: Path) -> None:
    """``warehouse/config.yml`` and friends are surfaced via the typed
    instance dump, so the data-layer pass must skip them to avoid
    double-printing.
    """
    instance_dir = _write_repo(
        tmp_path,
        data_files={
            "warehouse/config.yml": "size: Small\n",
            "data/keep.yml": "value: 1\n",
        },
    )
    view = build_authored_view(tmp_path, instance=None, target_name="dev", include_data=True)
    assert list(view["data"]) == ["data/keep.yml"]
    # Sanity: the file is on disk; we just chose not to render it.
    assert (instance_dir / "warehouse" / "config.yml").exists()


def test_authored_view_rejects_unknown_target(tmp_path: Path) -> None:
    _write_repo(tmp_path)
    with pytest.raises(FoundryConfigError) as excinfo:
        build_authored_view(tmp_path, instance=None, target_name="prod")
    msg = str(excinfo.value)
    assert "prod" in msg
    assert "Available targets" in msg
    assert "dev" in msg


def test_authored_view_requires_instance_when_multiple(tmp_path: Path) -> None:
    """With two ``[[instances]]`` entries, the caller must disambiguate."""
    _write_repo(
        tmp_path,
        extra_repo_toml=textwrap.dedent(
            """

            [[instances]]
            path = "instances/other"
            """
        ),
    )
    # Create the second per-instance file so the loader doesn't trip
    # on the missing one before reaching the "which instance?" check.
    (tmp_path / "instances" / "other").mkdir(parents=True)
    (tmp_path / "instances" / "other" / "foundry.toml").write_text(
        'name = "other"\nschema_prefix = "other_app"\n', encoding="utf-8"
    )

    with pytest.raises(FoundryConfigError):
        build_authored_view(tmp_path, instance=None, target_name="dev")


# ---------------------------------------------------------------------------
# build_resolved_view
# ---------------------------------------------------------------------------


def test_resolved_view_exposes_runtime_instance_config(tmp_path: Path) -> None:
    """The resolved view should round-trip the full ``InstanceConfig``
    dataclass: identity fields, target block, agent/warehouse/eval
    defaults, and the merged variable bag.
    """
    _write_repo(tmp_path)
    view = build_resolved_view(tmp_path, instance=None, target_name="dev")

    assert view["name"] == "demo"
    assert view["schema_prefix"] == "demo_app"
    # The runtime view carries the typed sub-configs as nested dicts.
    assert "target" in view
    assert view["target"]["catalog"] == "main"
    assert view["target"]["host"] == "https://dev.cloud.databricks.com"


def test_resolved_view_includes_data_with_substitutions(tmp_path: Path) -> None:
    _write_repo(
        tmp_path,
        data_files={"data/keep.yml": "schema: ${var.schema_prefix}_x\n"},
    )
    view = build_resolved_view(tmp_path, instance=None, target_name="dev", include_data=True)
    assert view["data"]["data/keep.yml"]["schema"] == "demo_app_x"


# ---------------------------------------------------------------------------
# render
# ---------------------------------------------------------------------------


def test_render_json_is_stable(tmp_path: Path) -> None:
    """JSON output is sorted + indented so diffs are reviewer-friendly."""
    _write_repo(tmp_path)
    view = build_authored_view(tmp_path, instance=None, target_name="dev")
    out = render(view, "json")
    assert out.endswith("\n")
    parsed = json.loads(out)
    assert parsed == view


def test_render_yaml_round_trips(tmp_path: Path) -> None:
    import yaml

    _write_repo(tmp_path)
    view = build_authored_view(tmp_path, instance=None, target_name="dev")
    parsed = yaml.safe_load(render(view, "yaml"))
    assert parsed == view


def test_render_toml_is_parsable_back(tmp_path: Path) -> None:
    """The TOML emitter must produce output the stdlib tomllib can
    read back -- otherwise the 'round-trippable authored view'
    promise is hollow.
    """
    import tomllib

    _write_repo(tmp_path)
    view = build_authored_view(tmp_path, instance=None, target_name="dev")
    rendered = render(view, "toml")
    reparsed = tomllib.loads(rendered)
    # Pick a few fields to assert; exhaustive equality is hard because
    # TOML doesn't model ``None`` and our writer emits "" instead.
    assert reparsed["schema_version"] == 2
    assert reparsed["target"]["name"] == "dev"
    assert reparsed["target"]["catalog"] == "main"
    assert reparsed["instance"]["name"] == "demo"


def test_render_unknown_format_raises() -> None:
    with pytest.raises(ValueError, match="Unknown format"):
        render({"a": 1}, "xml")  # type: ignore[arg-type]
